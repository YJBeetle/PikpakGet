"""Sequential grab pipeline: restore one file, download it, verify it, hand the
cloud space back, move on.

The order is forced by the drive quota, not by taste: a shared folder can be tens
of gigabytes while a free account has 6 GB of cloud space, and trashed bytes still
count against it. So each file gets its own restore → download → delete cycle, with
the plan written to disk before every side effect, which is what makes an
interrupted run resumable and a re-run harmless.
"""
import collections
import csv
import glob as _glob
import json
import os
import re
import itertools
import shutil
import time

from .api import PikPakError, Client, parse_share_url
from .stream import SegmentRefused, download_segments, download_stream

SPACE_HEADROOM = 400_000_000          # keep this much cloud space free
LOCAL_HEADROOM = 20_000_000_000       # ... and this much on the target volume
POLL_INTERVAL = 4
POLL_TIMEOUT = 1800
MAX_ATTEMPTS = 3
THROTTLE_STOP = 3                     # consecutive refusals before giving up

STATE_VERSION = 1


def job_key(job):
    """A link is identified by URL *and* destination folder, so the same share can
    be filed into two folders in one list without the second being skipped."""
    return f"{job['url']}\t{job.get('folder', '')}"


def human(nbytes):
    """Binary units, labelled as such.

    PikPak's own quota is 6442450944 bytes, i.e. exactly 6 GiB, so reporting the
    same number as "6 GB" understates it by 7% and makes two tools' figures refuse
    to reconcile."""
    value, unit = float(nbytes), 'B'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if abs(value) < 1024 or unit == 'PiB':
            return f'{value:.1f}{unit}'
        value /= 1024


def _clip_bytes(text, limit):
    """Cut a string to at most `limit` UTF-8 bytes without splitting a character."""
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode('utf-8', 'ignore')


def size_on_disk(path):
    """Size of a local file, or None when it is not there.

    A finished download is free to leave the library (that is the point of a
    library), and a moved file must not crash every later run on the same record."""
    if not path:
        return None
    try:
        return os.path.getsize(path) if os.path.isfile(path) else None
    except OSError:
        return None


def safe_name(name, limit=200):
    """Turn a remote file or folder name into one safe local path component,
    without tripping over NAME_MAX or path separators coming from the server."""
    text = re.sub(r'[/\x00]', '_', str(name)).strip()
    text = re.sub(r'\s+', ' ', text).strip()
    # a single leading dot is a dotfile and must survive; a double dot is traversal
    # at any position once separators have been flattened, so break every pair up
    text = text.replace('..', '_.')
    text = text.strip(' ')
    if len(text.encode('utf-8')) > limit:
        stem, dot, ext = text.rpartition('.')
        keep = max(limit - len(ext.encode('utf-8')) - 9, 8)
        if dot:
            text = f'{_clip_bytes(stem, keep)}….{ext}'
        else:
            text = _clip_bytes(text, limit - 3) + '…'
    return text or 'untitled'


def load_folder_map(path):
    """Optional `url,folder` mapping (CSV or TSV) used when the links file has no
    second column. The header line is skipped if it is not a link."""
    mapping = {}
    if not path or not os.path.exists(path):
        return mapping
    with open(path, encoding='utf-8-sig', newline='') as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = '\t' if '\t' in sample.splitlines()[0] else ','
        for row in csv.reader(handle, delimiter=delimiter):
            if len(row) < 2 or '://' not in row[0]:
                continue
            try:
                share_id, _ = parse_share_url(row[0])
            except ValueError:
                continue
            mapping[share_id] = row[1].strip()
    return mapping


def read_links(path, folder_map=None, default_folder='(unfiled)'):
    """Parse the ordered job list: one share link per line, optionally followed by
    a tab and the destination folder name. Returns (jobs, problems)."""
    folder_map = folder_map or {}
    jobs, problems = [], []
    with open(path, encoding='utf-8') as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip('\n').strip()
            if not line or line.startswith('#'):
                continue
            override = ''
            if '\t' in line:
                line, override = (part.strip() for part in line.split('\t', 1))
            try:
                share_id, pass_code = parse_share_url(line)
            except ValueError as error:
                problems.append(f'{path}:{number}: {error}')
                continue
            folder = override or folder_map.get(share_id) or default_folder
            job = {'url': line.split('?')[0].rstrip('/'), 'share_id': share_id,
                   'pass_code': pass_code, 'folder': safe_name(folder),
                   'order': len(jobs) + 1}
            job['key'] = job_key(job)
            jobs.append(job)
    return jobs, problems


class Log:
    def __init__(self, path=None, quiet=False):
        self.quiet = quiet
        self.handle = None
        if path:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            self.handle = open(path, 'a', encoding='utf-8')

    def __call__(self, message, level='info'):
        stamp = time.strftime('%H:%M:%S')
        debug_on = os.environ.get('PIKPAKGET_DEBUG')
        if level == 'debug' and not debug_on:
            return
        # --quiet still surfaces warnings, otherwise a stalled run looks healthy
        if not (self.quiet and level not in ('warn', 'error')):
            print(f'{stamp} {level.upper()[:4]} {message}', flush=True)
        if self.handle:
            self.handle.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} '
                              f'{level.upper()[:4]} {message}\n')
            self.handle.flush()


class State:
    """`state.json` holds the plan and per-file progress. Written atomically, and
    a file that cannot be parsed is moved aside rather than silently ignored."""

    def __init__(self, path):
        self.path = path
        self.data = {'version': STATE_VERSION, 'updated': None, 'links': {}, 'files': {}}
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as handle:
                    loaded = json.load(handle)
                if loaded.get('version') == STATE_VERSION and isinstance(loaded.get('files'), dict):
                    self.data = loaded
            except (ValueError, OSError) as error:
                backup = f'{path}.corrupt-{time.strftime("%Y%m%d-%H%M%S")}'
                shutil.copy2(path, backup)
                print(f'状态文件无法解析（{error}），已备份到 {backup}，本次从头规划')

    def link(self, url):
        return self.data['links'].setdefault(url, {'status': 'pending', 'attempts': 0})

    def file(self, file_id, url=None):
        return self.data['files'].setdefault(file_id, {
            'state': 'pending', 'attempts': 0, 'url': url, 'restored_id': None,
            'local': None, 'size': 0, 'seconds': None, 'error': None})

    def files_of(self, url):
        return [rec for rec in self.data['files'].values() if rec.get('url') == url]

    def reset_transient_failures(self):
        """Give files that failed last time a full budget again.

        `attempts` decides when a file is abandoned; accumulating it across runs
        would let three transient hiccups anywhere in a file's life condemn it
        permanently, after which every re-run skips it on the first glitch."""
        for record in self.data['files'].values():
            if record.get('state') in ('failed', 'retry'):
                record['state'] = 'pending'
                record['attempts'] = 0
        for info in self.data['links'].values():
            if info.get('status') == 'error':
                info['status'] = 'pending'
                info['attempts'] = 0

    def save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        self.data['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        tmp = f'{self.path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class Pipeline:
    def __init__(self, args, log, stop=lambda: False):
        self.args = args
        self.log = log
        self.stop = stop
        os.makedirs(args.state_dir, exist_ok=True)
        self.client = Client(session_path=os.path.join(args.state_dir, 'session.json'),
                             device_id_path=os.path.join(args.state_dir, 'device_id'),
                             logger=log)
        self.state = State(os.path.join(args.state_dir, 'state.json'))
        self.created = set()          # drive ids we made: the only ones we may delete
        for rec in self.state.data['files'].values():
            if rec.get('restored_id') and rec.get('state') not in ('done', 'too_big'):
                self.created.add(rec['restored_id'])
        self.quota_limit = 0
        # (folder, filename) -> source path inside the share, so two files that
        # happen to share a name inside one cluster cannot overwrite each other
        self.used_names = {}
        for rec in self.state.data['files'].values():
            if rec.get('local'):
                self.used_names[(os.path.dirname(rec['local']),
                                 os.path.basename(rec['local']))] = rec.get('path', '')
        self.files_left = args.max_files or 1 << 30
        self.files_done = 0
        self.bytes_done = 0
        self.throttle_hits = 0
        self.starved_files = 0

    # -------------------------------------------------------------------- quota
    def space(self):
        return self.client.space()

    def wait_space_freed(self, expect_drop, before=None, timeout=150):
        """The server is seconds behind `batchDelete`, and until the bytes come
        back the next restore will not fit."""
        floor = max((before or 0) - expect_drop, 0)
        # accept a near-miss: an unrelated copy parked in the drive (an interrupted
        # file that a later pass will reuse) keeps the absolute figure from ever
        # landing exactly, and waiting it out costs 150s per file for nothing
        tolerance = max(expect_drop * 0.02, 2_000_000)
        deadline = time.time() + timeout
        while time.time() < deadline:
            space = self.space()
            if space['usage'] <= floor + tolerance:
                return space
            time.sleep(POLL_INTERVAL)
        space = self.space()
        self.log(f'云端空间未在 {timeout}s 内回落，当前占用 {human(space["usage"])}', 'warn')
        return space

    def sweep_leftovers(self):
        """At startup nothing is in flight, so this is the only safe moment to drop
        the cloud copies an earlier interrupted run left behind: they would
        otherwise occupy quota and make every later space check stall. Only ids that
        our own state recorded are touched."""
        tracked = {rec['restored_id']: key for key, rec in self.state.data['files'].items()
                   if rec.get('restored_id') and rec['state'] not in ('done', 'too_big')}
        if not tracked:
            return
        victims = [item for item in self.client.list_folder('*')
                   if item.get('kind') == 'drive#file' and item.get('id') in tracked]
        if not victims:
            return
        size = sum(int(item.get('size') or 0) for item in victims)
        self.log(f'回收上次中断留下的 {len(victims)} 个云端副本（{human(size)}），'
                 '本地已完成的文件不受影响', 'info')
        before = self.space()['usage']          # the reclaim baseline, not the size
        self.client.cleanup([item['id'] for item in victims])
        for item in victims:
            self.state.data['files'][tracked[item['id']]]['restored_id'] = None
            self.created.discard(item['id'])
        self.state.save()
        self.wait_space_freed(size, before)

    def note_throttle(self, error):
        """Decide whether to stop the run, with advice that matches the cause.

        A rate limit, a refused byte range and an expired session all arrive as a
        failed file, but they want opposite responses: back off, keep going on one
        connection, or re-login."""
        status = getattr(error, 'status', None)
        if getattr(error, 'throttled', False) or status in (429, 503):
            self.throttle_hits += 1
            if self.throttle_hits >= THROTTLE_STOP:
                self.log(f'连续 {self.throttle_hits} 次被限流，停止本轮以免触发风控；'
                         '请至少 1 小时后再重跑同一命令（进度已保存）', 'error')
                return True
            return False
        if status in (401, 403) and not isinstance(error, SegmentRefused):
            # a dead session looks exactly like a refusal, and "wait an hour" would
            # send the user the wrong way
            self.log(f'凭据或会话被拒（HTTP {status}），请重新运行 --login 后重跑同一命令',
                     'error')
            return True
        return False

    # ----------------------------------------------------------------- planning
    def inventory(self, job):
        walked = self.client.walk_share(job['share_id'], job['pass_code'])
        files = [node for node in walked['nodes'] if not node['is_folder']]
        return {'title': walked.get('title') or '', 'token': walked['pass_code_token'],
                'files': files, 'total': sum(node['size'] for node in files),
                'folders': sum(1 for node in walked['nodes'] if node['is_folder']),
                'truncated': bool(walked.get('truncated'))}

    # ------------------------------------------------------------- one file step
    def reuse_or_restore(self, job, node, record, token):
        """Reuse an interrupted run's cloud copy only if it is provably the right
        file; otherwise drop it and restore again."""
        restored = record.get('restored_id')
        if restored:
            try:
                info = self.client.file(restored)
                exact = (info.get('kind') == 'drive#file'
                         and int(info.get('size') or 0) == node['size']
                         and not info.get('trashed'))
            except PikPakError:
                exact = False
            if exact:
                self.created.add(restored)
                return restored
            self.forget([restored], node['size'])
            record['restored_id'] = None
        return self.restore_one(job, node, record, token)

    def restore_one(self, job, node, record, token):
        """Restore one file id. The reply's `file_id` is not the copy that was just
        made, so the new file is identified by diffing the listing and matching
        its size."""
        snapshot = {item['id'] for item in self.client.list_folder('*')}
        made = self.client.restore(job['share_id'], token, [node['id']], '*')
        record['restore_task_id'] = made.get('restore_task_id')
        status = str(made.get('restore_status') or '')
        if 'FAIL' in status.upper() or 'ERROR' in status.upper():
            raise PikPakError(f'转存被拒: {status} {json.dumps(made, ensure_ascii=False)[:200]}')
        deadline = time.time() + POLL_TIMEOUT
        while True:
            fresh = [item for item in self.client.list_folder('*')
                     if item['id'] not in snapshot and item.get('kind') == 'drive#file'
                     and not item.get('trashed')
                     and (not node['size'] or int(item.get('size') or 0) == node['size'])]
            if fresh:
                restored = fresh[0]['id']
                self.created.add(restored)
                record['restored_id'] = restored
                self.state.save()
                return restored
            if time.time() > deadline:
                raise PikPakError(f'转存后 {POLL_TIMEOUT // 60} 分钟内没看到新文件')
            time.sleep(POLL_INTERVAL)

    def wait_ready(self, file_id, expect_size):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            info = self.client.file(file_id)
            phase = str(info.get('phase') or '')
            size = int(info.get('size') or 0)
            if 'FAIL' in phase.upper():
                raise PikPakError(f'云端文件状态异常 {phase}')
            if phase.endswith('COMPLETE') or (expect_size and size == expect_size):
                if expect_size and size and size != expect_size:
                    raise PikPakError(f'云端文件大小不符 {size} != {expect_size}')
                return info
            time.sleep(POLL_INTERVAL)
        raise PikPakError('等待云端文件就绪超时')

    def dest_path(self, node, dest_dir):
        """Final path for a share node, made unique inside the cluster folder.

        Names come from someone else's folder layout, so collisions are normal;
        the chosen name is remembered in state, which keeps a resumed run writing
        to the same file instead of creating a second copy of it."""
        name = safe_name(node['name'])
        taken = self.used_names.get((dest_dir, name))
        if taken and taken != node['path']:
            parent = node['path'].rsplit('/', 2)[-2] if '/' in node['path'] else ''
            name = safe_name(f'{parent} - {node["name"]}' if parent else node['path'])
            bump = 2
            while self.used_names.get((dest_dir, name)) not in (None, node['path']):
                stem, dot, ext = name.rpartition('.')
                name = f'{stem} ({bump}).{ext}' if dot else f'{name} ({bump})'
                bump += 1
            self.log(f'同名冲突，另存为 {name}', 'debug')
        self.used_names[(dest_dir, name)] = node['path']
        return os.path.join(dest_dir, name)

    def _we_wrote(self, path):
        """Whether state records this exact file as something we downloaded.

        Only our own output may be replaced wholesale; a same-named file the user
        placed in the library themselves is preserved (see fetch_to)."""
        return any(record.get('local') == path
                   and record.get('state') in ('downloaded', 'done')
                   for record in self.state.data['files'].values())

    def fetch_to(self, file_id, node, dest_dir):
        """Stream the file to its final folder. Downloading straight into the
        destination (via `.part`) means an interrupted run never leaves a partial
        file that looks complete."""
        os.makedirs(dest_dir, exist_ok=True)
        final = self.dest_path(node, dest_dir)
        part = f'{final}.part'
        expected = node['size']
        # isfile, not exists: a stray directory at that path is not a finished file
        if os.path.isfile(final):
            local = os.path.getsize(final)
            if expected and local == expected:
                self.log(f'已存在且大小一致，跳过 {node["name"][:40]} ({human(local)})')
                return final, 'existing'
            if not self._we_wrote(final):
                # anything else sitting in the library belongs to the user: a share
                # reusing that filename is not licence to delete it
                clash = f'{final}.conflict-{time.strftime("%Y%m%d-%H%M%S")}'
                os.rename(final, clash)
                self.log(f'目标同名文件不是本工具下的，改名让路 '
                         f'{os.path.basename(clash)[:40]}', 'warn')
            else:
                self.log(f'已存在但大小不符（{human(local)} != {human(expected)}），重下', 'warn')
                os.remove(final)
        seg_dir = f'{part}.segs{self.args.connections}'
        # a half-finished single-stream .part keeps its progress: resume it as-is
        # instead of restarting the file over segments
        legacy = (os.path.exists(part) and os.path.getsize(part) > 0
                  and not os.path.isdir(seg_dir))
        if self.args.connections > 1 and expected and not legacy:
            try:
                got = download_segments(lambda: self.client.download_url(file_id)[0], part,
                                        expected, self.args.connections, stop=self.stop,
                                        log=self.log)
            except SegmentRefused as error:
                # stop knocking on a refused door: finish this file on one
                # connection and stay there for the rest of the run
                self.log(f'{error}；本轮后续改用单连接', 'warn')
                self.args.connections = 1
                shutil.rmtree(seg_dir, ignore_errors=True)
            except PikPakError as error:
                # starved lanes cost 60+300+900s of back-off per file for bytes one
                # connection would have delivered sooner; after two such files, stop
                # paying that tuition for the rest of the run
                if '分段多次未完成' not in str(error):
                    raise
                self.starved_files += 1
                self.log(f'{error}；本文件改走单连接'
                         + ('，后续所有文件也用单连接' if self.starved_files >= 2 else ''), 'warn')
                if self.starved_files >= 2:
                    self.args.connections = 1
                shutil.rmtree(seg_dir, ignore_errors=True)
            else:
                if got != expected:
                    raise PikPakError(f'拼装后大小不符 {got}/{expected}')
                os.replace(part, final)
                return final, 'downloaded'
        resume = os.path.getsize(part) if os.path.exists(part) else 0
        url = self.client.download_url(file_id)[0]
        written = download_stream(url, part, expected, resume, stop=self.stop)
        if expected and written != expected:
            raise PikPakError(f'下载不完整 {written}/{expected}')
        os.replace(part, final)
        return final, 'downloaded'

    def forget(self, ids, size=0):
        """Remove copies we created. Anything not ours is left alone, always."""
        mine = [item for item in ids if item and item in self.created]
        if not mine or self.args.no_delete:
            return
        before = self.space()['usage']
        self.client.cleanup(mine)
        self.created.difference_update(mine)
        self.wait_space_freed(size, before)

    # --------------------------------------------------------------------- drive
    def run_link(self, job):
        key = job.get('key') or job_key(job)
        link = self.state.link(key)
        link['order'] = job['order']
        link.setdefault('folder', job['folder'])
        if link['status'] == 'done':
            self.log(f'跳过已完成: {job["folder"]} {job["share_id"][-8:]}')
            return 'done'
        try:
            report = self.inventory(job)
        except PikPakError as error:
            link.update({'status': 'error', 'error': str(error)[:300],
                         'attempts': link['attempts'] + 1})
            self.state.save()
            self.log(f'链接不可用 {job["share_id"][-8:]}: {error}', 'error')
            return 'throttled' if self.note_throttle(error) else 'error'
        files = sorted(report['files'], key=lambda node: node['size'])
        link.update({'status': 'active', 'folder': job['folder'], 'title': report['title'],
                     'total_bytes': report['total'], 'file_count': len(files),
                     'truncated': report['truncated']})
        if report['truncated']:
            # a capped listing must not be mistaken for "everything is done"
            self.log(f'{job["folder"]}: 枚举被 max_nodes 截断，可能有文件未列入，'
                     '请加大上限后重跑', 'warn')
        self.state.save()
        biggest = max((node['size'] for node in files), default=0)
        self.log(f'{job["folder"]}: {len(files)} 个文件 / {human(report["total"])}，'
                 f'最大单文件 {human(biggest)}')
        if self.args.dry_run:
            return 'planned'
        dest_dir = os.path.join(self.args.dest, job['folder'])
        for position, node in enumerate(files, 1):
            if self.stop() or self.files_left <= 0:
                link['status'] = 'partial'
                self.state.save()
                return 'stopped'
            record = self.state.file(node['id'], key)
            record.update({'name': node['name'], 'size': node['size'], 'path': node['path'],
                           'local': record.get('local') or self.dest_path(node, dest_dir)})
            if record['state'] == 'done' and record.get('local') and os.path.exists(record['local']):
                continue
            if record['state'] == 'downloaded' and size_on_disk(record.get('local')) == node['size']:
                self.forget([record.get('restored_id')], node['size'])
                record['state'] = 'done'
                self.state.save()
                self.log(f'补做云端清理: {node["name"][:44]}')
                continue
            if self.quota_limit and node['size'] > self.quota_limit - SPACE_HEADROOM:
                # restoring this would fail half-way and leave the drive full
                record.update({'state': 'too_big',
                               'error': f'单文件 {human(node["size"])} 超过云盘配额'})
                self.state.save()
                self.log(f'  [{position}/{len(files)}] 跳过：{human(node["size"])} 超过云盘配额 '
                         f'{node["name"][:40]}', 'warn')
                continue
            space = self.space()
            if space['limit'] and space['free'] < node['size'] + SPACE_HEADROOM:
                self.log(f'云盘空间不足：需要 {human(node["size"])}，可用 {human(space["free"])}'
                         f'（回收站占 {human(space["in_trash"])}）', 'error')
                if not self.purge_trash():
                    link['status'] = 'partial'
                    self.state.save()
                    return 'blocked'
            if shutil.disk_usage(self.args.dest).free < node['size'] + LOCAL_HEADROOM:
                self.log(f'目标盘空间不足（还差 {human(node["size"])}），停止', 'error')
                link['status'] = 'partial'
                self.state.save()
                return 'blocked'
            if self.files_done and self.args.gap:
                time.sleep(self.args.gap)
            self.files_left -= 1
            self.log(f'  [{position}/{len(files)}] {human(node["size"])} {node["path"][:70]}')
            try:
                started = time.time()
                record['state'] = 'restoring'
                record['attempts'] += 1
                self.state.save()
                restored = self.reuse_or_restore(job, node, record, report['token'])
                self.wait_ready(restored, node['size'])
                local, how = self.fetch_to(restored, node, dest_dir)
                record.update({'state': 'downloaded', 'local': local,
                               'seconds': round(time.time() - started, 1)})
                self.state.save()
                self.forget([restored], node['size'])
                record['state'] = 'done'
                record['error'] = None
                self.state.save()
                self.files_done += 1
                self.bytes_done += node['size']
                self.throttle_hits = 0      # the counter means *consecutive*
                self.log(f'    {how}，云端已清理 -> {os.path.basename(local)[:56]}')
            except (PikPakError, OSError) as error:
                record['state'] = 'failed' if record['attempts'] >= MAX_ATTEMPTS else 'retry'
                record['error'] = str(error)[:300]
                link['status'] = 'partial'
                self.state.save()
                self.log(f'    失败（{record["state"]}）: {error}', 'warn')
                try:
                    self.forget([record.get('restored_id')], node['size'])
                except PikPakError as cleanup_error:      # keep the reason, not the noise
                    self.log(f'    云端清理也没成功，稍后重试: {cleanup_error}', 'warn')
                if self.note_throttle(error):
                    return 'throttled'
                if record['state'] == 'failed':
                    continue
                return 'retry'
        pending = [rec for rec in self.state.files_of(key)
                   if rec['state'] not in ('done', 'too_big')]
        link['status'] = 'done' if not pending else 'incomplete'
        link['pending_files'] = len(pending)
        self.state.save()
        if pending:
            self.log(f'{job["folder"]}: 仍有 {len(pending)} 个文件未完成', 'warn')
        return 'ok' if not pending else 'incomplete'

    def purge_trash(self):
        """Empty the trash to reclaim quota — only when the caller opted in, since
        it is account-wide and cannot be undone."""
        if not self.args.purge_trash:
            return False
        doomed = self.client.list_trash()
        self.log(f'清空回收站：{len(doomed)} 项（'
                 f'{human(sum(int(item.get("size") or 0) for item in doomed))}）', 'warn')
        self.client.empty_trash()
        time.sleep(POLL_INTERVAL)
        return True

    def run(self, jobs):
        os.makedirs(self.args.dest, exist_ok=True)
        if not os.access(self.args.dest, os.W_OK):
            self.log(f'目标目录不可写: {self.args.dest}', 'error')
            return 2
        if not self.args.dry_run:
            self.state.reset_transient_failures()
            self.state.save()
        if not self.args.no_sweep:
            self.sweep_leftovers()
        space = self.space()
        self.quota_limit = space['limit']
        self.log(f'{len(jobs)} 个链接 -> {self.args.dest} | 云盘 '
                 f'{human(space["usage"])}/{human(space["limit"])} 已用'
                 f'（回收站 {human(space["in_trash"])}）| '
                 f'本地可用 {human(shutil.disk_usage(self.args.dest).free)} | '
                 f'并发 {self.args.connections} 段')
        if self.args.inventory_only:
            return self.report(jobs)
        if self.args.limit:
            jobs = jobs[:self.args.limit]
        for pass_number in itertools.count(1):
            if self.args.repeat and pass_number > self.args.repeat:
                break
            tally, before_done, before_bytes = collections.Counter(), self.files_done, self.bytes_done
            for job in jobs:
                if self.stop():
                    break
                self.log(f'— 第{pass_number}轮 [{job["order"]}/{len(jobs)}] {job["url"]}')
                tally[self.run_link(job)] += 1
                if tally.get('throttled') or self.stop():
                    break
            self.log(f'第 {pass_number} 轮结束：{dict(tally)} | '
                     f'云盘占用 {human(self.space()["usage"])}')
            # only this run's list: leftovers from earlier link files, or links the
            # user has since dropped, must not force another pass and a non-zero exit
            unfinished = [job.get('key') or job_key(job) for job in jobs
                          if self.state.link(job.get('key') or job_key(job))['status'] != 'done']
            if not unfinished:
                return 0
            if self.files_done == before_done and self.bytes_done == before_bytes:
                # nothing landed this pass: circling again would only burn quota and
                # goodwill, so stop and let a human look
                self.log(f'{len(unfinished)} 个链接仍未完成，且本轮零进展，停止重试。'
                         '请看上面的失败原因后重跑同一命令', 'error')
                return 1
            self.log(f'{len(unfinished)} 个链接未完成，进入下一轮补齐')
        self.log('重跑同一命令即可从断点继续')
        return 0

    def report(self, jobs):
        rows = []
        for job in jobs:
            try:
                report = self.inventory(job)
            except PikPakError as error:
                self.log(f'{job["share_id"][-10:]}: {error}', 'warn')
                rows.append({'folder': job['folder'], 'share_id': job['share_id'],
                             'files': 0, 'bytes': 0, 'largest': 0, 'unfetchable': 0,
                             'title': '', 'error': str(error)[:160]})
                continue
            files = report['files']
            largest = max((node['size'] for node in files), default=0)
            rows.append({'folder': job['folder'], 'share_id': job['share_id'],
                         'files': len(files), 'bytes': report['total'], 'largest': largest,
                         'unfetchable': sum(1 for node in files
                                            if node['size'] > self.quota_limit - SPACE_HEADROOM),
                         'title': report['title'], 'error': ''})
            self.log(f'  {job["folder"]:<24}{len(files):>5} 个  {human(report["total"]):>10}'
                     f'  最大 {human(largest)}')
        fields = ('folder', 'share_id', 'files', 'bytes', 'largest', 'unfetchable', 'title', 'error')
        directory = os.path.dirname(self.args.inventory_out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.args.inventory_out, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        total = sum(row['bytes'] for row in rows)
        blocked = sum(row['unfetchable'] for row in rows)
        self.log(f'清单写出 {self.args.inventory_out}: {len(rows)} 链接 / {human(total)}'
                 f' | 单文件超过云盘配额、无法下载 {blocked} 个')
        return 0

    def status(self):
        """Per-folder progress plus an ETA from the throughput this run actually
        got, not a theoretical one."""
        by_url = collections.defaultdict(list)
        for rec in self.state.data['files'].values():
            by_url[rec.get('url')].append(rec)
        print(f"{'folder':<26}{'status':<12}{'files':>7}{'done':>6}"
              f"{'bytes':>11}{'planned':>11}  progress")
        totals = {'got': 0, 'planned': 0, 'spent': 0.0, 'skipped': 0}
        for url, info in sorted(self.state.data['links'].items(),
                                key=lambda kv: kv[1].get('order') or 999):
            records = by_url.get(url, [])
            planned = sum(rec['size'] for rec in records)
            got = sum(rec['size'] for rec in records if rec['state'] == 'done')
            spent = sum(rec.get('seconds') or 0 for rec in records if rec['state'] == 'done')
            skipped = sum(rec['size'] for rec in records if rec['state'] == 'too_big')
            over = sum(1 for rec in records if rec['state'] == 'too_big')
            done = sum(1 for rec in records if rec['state'] == 'done')
            share = (got / planned * 100) if planned else 0
            note = f'  ({over} over quota)' if over else ''
            folder = (info.get('folder') or url[-10:])[:25]
            print(f'{folder:<26}{(info.get("status") or "-"):<12}{len(records):>7}{done:>6}'
                  f'{human(got):>11}{human(planned):>11}  {share:5.1f}% '
                  f"{'#' * int(share / 4)}{note}")
            totals['got'] += got
            totals['planned'] += planned
            totals['spent'] += spent
            totals['skipped'] += skipped
        remaining = max(totals['planned'] - totals['got'] - totals['skipped'], 0)
        if totals['spent'] and totals['got']:
            rate = totals['got'] / totals['spent']
            hours = remaining / rate / 3600 if rate else 0
            print(f"合计 {human(totals['got'])} / {human(totals['planned'])}，"
                  f"实测 {human(rate)}/s，剩余 {human(remaining)} 约需 "
                  f'{hours:.0f} 小时（{hours / 24:.1f} 天）')
        for path in sorted(_glob.glob(os.path.join(self.args.dest, '*', '*.part'))):
            age = time.time() - os.path.getmtime(path)
            print(f'下载中: {os.path.basename(path)[:52]}… {human(os.path.getsize(path))}'
                  f'（{age:.0f}s 前还在写）')
        try:
            space = self.space()
            print(f'云盘 {human(space["usage"])}/{human(space["limit"])} 已用，'
                  f'本地剩余 {human(shutil.disk_usage(self.args.dest).free)}')
        except PikPakError as error:
            print(f'云盘配额查询失败: {error}')
        return 0
