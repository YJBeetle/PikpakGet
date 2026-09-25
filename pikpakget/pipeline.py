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
import sys
import time

from .api import (DOT_DIR_NAME, Client, PikPakError, TrafficCapped, account_dir,
                 parse_share_url)
from .stream import (PIECE_SIZES, SegmentRefused, download_segments, download_stream,
                     verify_content)

SPACE_HEADROOM = 400_000_000          # keep this much cloud space free
LOCAL_HEADROOM = 20_000_000_000       # ... and this much on the target volume
POLL_INTERVAL = 4
POLL_TIMEOUT = 1800
MAX_ATTEMPTS = 3
QUARANTINE_SUFFIX = '.unverified'     # bytes we keep but cannot vouch for
THROTTLE_STOP = 3                     # consecutive refusals before giving up
AUTH_STOP = 3                         # ... and consecutive 401/403s before blaming the session

STATE_VERSION = 3


def job_key(job):
    """A link is identified by URL *and* destination folder, so the same share can
    be filed into two folders in one list without the second being skipped."""
    return f"{job['url']}\t{job.get('folder', '')}"


def file_key(link_key, file_id):
    return f'{link_key}\t{file_id}'


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


def disk_free(path):
    """Bytes free on the volume that would hold `path`, which need not exist yet.

    statvfs on a missing directory raises FileNotFoundError, and a read-only command
    like `--status` must not create `--dest` just to measure it — a fresh machine that
    has never downloaded anything is the normal case there."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:                       # climbed to a root with nothing above
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


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
    # only a name that *is* '.' or '..' names another directory; once separators are
    # flattened a pair of dots inside a title is just two dots, and rewriting those
    # mangled filenames for protection nobody needed
    if text in ('.', '..'):
        text = '_' + text
    # a library keeps its journal in <dest>/.pikpakget, so a share folder or file that
    # arrives with exactly that name would otherwise be written into (or over) it
    if text == DOT_DIR_NAME:
        text = '_' + text
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

    def close(self):
        # idempotent: main closes the log it opened on every exit path, while a
        # library caller keeps writing to the one it built itself
        handle, self.handle = self.handle, None
        if handle:
            handle.close()

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
            except (ValueError, OSError) as error:
                backup = f'{path}.corrupt-{time.strftime("%Y%m%d-%H%M%S")}'
                shutil.copy2(path, backup)
                # stderr, so it cannot pollute a machine-read progress listing
                print(f'状态文件无法解析（{error}），已备份到 {backup}，本次从头规划',
                      file=sys.stderr)
            else:
                if not isinstance(loaded, dict) or loaded.get('version') != STATE_VERSION:
                    raise PikPakError(f'状态版本不受支持：{path}；请自行移走旧文件后重跑')
                if not isinstance(loaded.get('links'), dict) or not isinstance(loaded.get('files'), dict):
                    raise PikPakError(f'状态结构不完整：{path}；请先检查文件')
                self.data = loaded

    def link(self, url):
        return self.data['links'].setdefault(url, {'status': 'pending', 'attempts': 0})

    def file(self, file_id, url=None):
        key = file_key(url, file_id) if url is not None else file_id
        if url is None and key not in self.data['files']:
            matches = [rec for rec in self.data['files'].values()
                       if rec.get('source_id') == file_id]
            if len(matches) == 1:
                return matches[0]
        return self.data['files'].setdefault(key, {
            'state': 'pending', 'attempts': 0, 'url': url, 'restored_id': None,
            'source_id': file_id,
            'local': None, 'home': None, 'quarantined': None,
            'size': 0, 'seconds': None, 'error': None})

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
    def __init__(self, args, log, stop=lambda: False, client=None,
                 account_id=None, workspace_id=None):
        self.args = args
        self.log = log
        self.stop = stop
        # no makedirs here: State.save and the lock create the journal directory when
        # something has to be written, and a read-only report must not fail because a
        # library path turned out to be unbuildable
        #
        # The CLI supplies the selected account's client. Direct callers receive an
        # in-memory, unauthenticated client and cannot accidentally use an old login.
        self.account_dir = getattr(args, 'account_dir', None) or account_dir()
        self.client = client or Client(
            session_path=None,
            device_id_path=os.path.join(self.account_dir, 'device_id'), logger=log)
        self.account_id = account_id
        self.workspace_id = workspace_id
        self.traffic_capped = False
        self.state = State(os.path.join(args.state_dir, 'state.json'))
        if account_id and workspace_id:
            # prepare_workspace emptied this account's staging folder before we got
            # here; its old cloud ids cannot be resumed or cleaned again.
            for rec in self.state.data['files'].values():
                if rec.get('account_id') == account_id:
                    rec['restored_id'] = None
        self.created = set()          # drive ids we made: the only ones we may delete
        for rec in self.state.data['files'].values():
            if (rec.get('restored_id') and rec.get('state') not in ('done', 'too_big')
                    and (not account_id or rec.get('account_id') == account_id)):
                self.created.add(rec['restored_id'])
        self.quota_limit = 0
        # (folder, filename) -> source path inside the share, so two files that
        # happen to share a name inside one cluster cannot overwrite each other
        self._case_folded = None
        self.used_names = {}
        for rec in self.state.data['files'].values():
            if rec.get('local'):
                self.used_names[self._name_key(os.path.dirname(rec['local']),
                                               os.path.basename(rec['local']))] = rec.get('path', '')
        self.files_left = args.max_files or 1 << 30
        self.files_done = 0
        self.bytes_done = 0
        self.throttle_hits = 0
        self.auth_hits = 0
        self.hash_hint = None           # block size the last file verified at
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

    def note_throttle(self, error):
        """Decide whether to stop the run, with advice that matches the cause.

        A rate limit, a refused byte range and an expired session all arrive as a
        failed file, but they want opposite responses: back off, keep going on one
        connection, or re-login."""
        status = getattr(error, 'status', None)
        if self.client.session_dead:
            self.log('会话续期被服务器拒绝，停止本轮：请重新运行 --login '
                     '后重跑同一命令（进度已保存）', 'error')
            return True
        if isinstance(error, TrafficCapped):
            self.traffic_capped = True
            self.log('该账号今日下行流量已用满，停止使用此账号；进度已保存', 'error')
            return True
        if getattr(error, 'throttled', False) or status in (429, 503):
            self.throttle_hits += 1
            if self.throttle_hits >= THROTTLE_STOP:
                self.log(f'连续 {self.throttle_hits} 次被限流，停止本轮以免触发风控；'
                         '请至少 1 小时后再重跑同一命令（进度已保存）', 'error')
                return True
            return False
        if status in (401, 403) and not isinstance(error, SegmentRefused):
            # PikPak answers 403 both for a dead session and for one share that has
            # since been revoked or reported, and they look identical here. Dropping
            # that single link costs the rest of a multi-day run nothing, so stop only
            # once the refusals are the common factor rather than the exception.
            self.auth_hits += 1
            if self.auth_hits >= AUTH_STOP:
                self.log(f'连续 {self.auth_hits} 次 HTTP {status} 被拒，更像会话或账号问题：'
                         '请重新运行 --login 后重跑同一命令（进度已保存）', 'error')
                return True
            self.log(f'HTTP {status} 被拒，按本链接不可用处理后继续'
                     f'（连续第 {self.auth_hits}/{AUTH_STOP} 次）', 'warn')
            return False
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
        if self.account_id and record.get('account_id') != self.account_id:
            restored = None
            record['restored_id'] = None
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
        parent_id = self.workspace_id or '*'
        snapshot = {item['id'] for item in self.client.list_folder(parent_id)}
        made = self.client.restore(job['share_id'], token, [node['id']], parent_id)
        record['restore_task_id'] = made.get('restore_task_id')
        status = str(made.get('restore_status') or '')
        if 'FAIL' in status.upper() or 'ERROR' in status.upper():
            raise PikPakError(f'转存被拒: {status} {json.dumps(made, ensure_ascii=False)[:200]}')
        deadline = time.time() + POLL_TIMEOUT
        while True:
            fresh = [item for item in self.client.list_folder(parent_id)
                     if item['id'] not in snapshot and item.get('kind') == 'drive#file'
                     and not item.get('trashed')
                     and (not node['size'] or int(item.get('size') or 0) == node['size'])
                     and item.get('name') == node['name']]
            if len(fresh) > 1:
                raise PikPakError('转存后出现多个同名同大小的新文件，无法确认副本归属')
            if fresh:
                restored = fresh[0]['id']
                self.created.add(restored)
                record['restored_id'] = restored
                record['account_id'] = self.account_id
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
        taken = self.used_names.get(self._name_key(dest_dir, name))
        if taken and taken != node['path']:
            parent = node['path'].rsplit('/', 2)[-2] if '/' in node['path'] else ''
            name = safe_name(f'{parent} - {node["name"]}' if parent else node['path'])
            bump = 2
            while self.used_names.get(self._name_key(dest_dir, name)) not in (None, node['path']):
                stem, dot, ext = name.rpartition('.')
                name = f'{stem} ({bump}).{ext}' if dot else f'{name} ({bump})'
                bump += 1
            self.log(f'同名冲突，另存为 {name}', 'debug')
        self.used_names[self._name_key(dest_dir, name)] = node['path']
        return os.path.join(dest_dir, name)

    def _name_key(self, dest_dir, name):
        return (dest_dir, name.casefold() if self.case_insensitive() else name)

    def case_insensitive(self):
        """Whether the destination volume folds letter case in filenames.

        macOS's default APFS and most SMB/CIFS mounts (the usual way a NAS share
        reaches a desktop) treat `Movie.mp4` and `movie.mp4` as the same path, while
        ext4 treats them as two. Comparing names exactly therefore hands both files
        the same destination on a folded volume: the second download overwrites the
        first and state records two finished files where one exists. Probed once,
        because a rename probe costs a filesystem round trip we do not want per file."""
        if self._case_folded is None:
            probe = 'pikpakget-case-probe.XX'
            try:
                os.makedirs(self.args.dest, exist_ok=True)
                with open(os.path.join(self.args.dest, probe), 'wb'):
                    pass
                self._case_folded = os.path.exists(os.path.join(self.args.dest,
                                                                probe.casefold()))
                os.remove(os.path.join(self.args.dest, probe))
            except OSError:
                self._case_folded = False      # cannot tell: assume the strict case
        return self._case_folded

    def fetch_to(self, file_id, node, dest_dir, record=None):
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
                if node.get('hash') and verify_content(final, node['hash'],
                                                       self.hash_sizes(record)):
                    self.log(f'已存在且内容 hash 一致，跳过 {node["name"][:40]}')
                    return final, 'existing'
            owned = (record is not None and record.get('local') == final
                     and record.get('state') in ('downloaded', 'done'))
            if not owned:
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
        partial_stream = (os.path.exists(part) and os.path.getsize(part) > 0
                          and not os.path.isdir(seg_dir))
        if self.args.connections > 1 and expected and not partial_stream:
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
                return self.promote(part, final, node, record)
        resume = os.path.getsize(part) if os.path.exists(part) else 0
        url = self.client.download_url(file_id)[0]
        written = download_stream(url, part, expected, resume, stop=self.stop)
        if expected and written != expected:
            raise PikPakError(f'下载不完整 {written}/{expected}')
        return self.promote(part, final, node, record)

    def verify(self, jobs):
        """Re-check every file the state claims to have downloaded, using the hash the
        share still reports, and quarantine the ones that fail.

        Quarantining is what makes "re-run the download command" the actual fix: the
        suspect bytes are renamed to `<name>.unverified` — kept as evidence, and
        because 500 MiB is not free to fetch twice — and the record goes back to
        `pending`, so the next pass writes a fresh copy under the original name. A
        file that was quarantined and now hashes out is moved back into place."""
        checked, suspect, absent, unknown, skipped = 0, [], [], 0, 0
        for job in jobs:
            try:
                report = self.inventory(job)
            except PikPakError as error:
                # one dead share must not stop the sweep over everything else
                skipped += 1
                self.log(f'{job["folder"]}: 读不到清单，这个链接暂时没法复核: {error}', 'warn')
                continue
            for node in report['files']:
                record = self.state.data['files'].get(file_key(job.get('key') or job_key(job),
                                                               node['id']))
                if not record or record.get('state') not in ('done', 'unverified'):
                    continue
                local = record.get('local')
                if not local or not os.path.exists(local):
                    absent.append(node['name'])
                    continue
                if not node['hash']:
                    unknown += 1
                    continue
                piece = None
                if size_on_disk(local) == node['size']:
                    piece = verify_content(local, node['hash'], self.hash_sizes(record))
                if piece is None:
                    self.quarantine(local, f"云端 {node['hash'][:16]}… 无候选分片命中", record)
                    # running --verify *is* the ask to fix it: the record goes back into
                    # the queue under its real name, while `quarantined` keeps the old
                    # bytes reachable until a good copy replaces them
                    record.update({'state': 'pending', 'local': record['home'], 'attempts': 0})
                    suspect.append(node['name'])
                    continue
                record.update({'hash_piece': piece, 'state': 'done', 'error': None})
                if record.get('quarantined') == local:
                    home = record['home']
                    os.replace(local, home)
                    record['local'] = home
                    record['quarantined'] = None
                    self.log(f'  隔离的文件复核通过，放回 {os.path.basename(home)[:52]}')
                checked += 1
        self.state.save()
        self.log(f'校验完成：{checked} 个内容 hash 相符，{len(suspect)} 个已隔离并退回重下，'
                 f'{len(absent)} 个本地不在，{unknown} 个云端没给 hash，'
                 f'{skipped} 个链接读不到清单')
        for name in absent:
            self.log(f'  本地没有：{name[:60]}', 'warn')
        return 1 if suspect else 0


    def hash_sizes(self, record=None):
        """Remembered block sizes first. One share is usually one client with one
        size, so the size this file (or the file before it) actually verified at beats
        starting the search at 1 MiB again for every 500 MiB."""
        for size in ((record or {}).get('hash_piece'), self.hash_hint):
            if size:
                return (size, *(candidate for candidate in PIECE_SIZES if candidate != size))
        return PIECE_SIZES

    def quarantine(self, path, reason, record=None, home=None):
        """Move bytes the server's hash does not explain out of the way **without
        deleting them**, and keep them reachable from the record.

        The hash rule is reverse engineered and the block size is the uploader's
        client's choice, so "no candidate reproduced it" is unproven rather than
        proven bad — and those bytes may be an hour of quota that nobody can fetch
        again on a whim. The `.unverified` name is what a human sees in the folder.

        `home` is the name the file is *supposed* to have. It has to be passed in
        rather than derived from `path`: at download time the bytes are still in a
        `.part`, and stripping the suffix later would hand the library a permanent
        `movie.mp4.part` that `fetch_to` mistakes for resumable progress. The doomed
        path also lives in its own record field, because a sweep that reported a file
        and dropped the reference left orphans nobody could find again."""
        home = home or path
        doomed = home + QUARANTINE_SUFFIX
        if os.path.exists(doomed):
            doomed = f'{doomed}-{time.strftime("%Y%m%d-%H%M%S")}'
        os.replace(path, doomed)
        if record is not None:
            record.update({'state': 'unverified', 'local': doomed, 'home': home,
                           'quarantined': doomed, 'hash_piece': None, 'error': reason[:300]})
        self.log(f'内容核对不过（{reason}），字节保留为 {os.path.basename(doomed)}', 'error')
        return doomed

    def promote(self, part, final, node, record=None):
        """Let the bytes become the real file only once the server's content hash
        reproduces, and remember which block size did it.

        While retries remain, a mismatch deletes the `.part`: the damage two writers
        on one segment leave behind is a file of the right length with a shifted
        middle, and refetching is what clears it. On the last attempt the bytes are
        quarantined instead of deleted, because a block size nobody has seen is not
        evidence of damage."""
        expected = (node or {}).get('hash')
        if expected:
            piece = verify_content(part, expected, self.hash_sizes(record))
            if piece is None:
                reason = f'云端 {expected[:16]}… 无候选分片命中'
                if record is not None and record.get('attempts', 0) >= MAX_ATTEMPTS:
                    return self.quarantine(part, reason, record, home=final), 'unverified'
                os.remove(part)
                raise PikPakError(f'内容 hash 与云端不符（{reason}），丢弃重下')
            if record is not None:
                record['hash_piece'] = piece
            self.hash_hint = piece
            self.log(f'内容 hash 已核对（{piece >> 10} KiB 分片）')
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
        # A share can gain files after a previous run. Re-list even completed links;
        # the per-file journal below skips files whose local copies are intact.
        try:
            report = self.inventory(job)
        except PikPakError as error:
            link.update({'status': 'error', 'error': str(error)[:300],
                         'attempts': link['attempts'] + 1})
            self.state.save()
            self.log(f'链接不可用 {job["share_id"][-8:]}: {error}', 'error')
            return 'throttled' if self.note_throttle(error) else 'error'
        # a listing that answers is proof the credentials work, so anything refused
        # further down is about that one file, not about the session
        self.auth_hits = 0
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
            previous_hash = record.get('source_hash')
            changed_source = bool(previous_hash and node.get('hash')
                                  and previous_hash != node['hash'])
            changed_account_without_hash = bool(
                self.account_id and record.get('account_id')
                and record['account_id'] != self.account_id and not node.get('hash'))
            if changed_source or changed_account_without_hash:
                # Range-resuming bytes from an unidentified source can produce a
                # right-sized splice of two different files.
                home = record.get('home') or record.get('local')
                if home:
                    partial = home + '.part'
                    if os.path.isfile(partial):
                        os.remove(partial)
                    for segments in _glob.glob(partial + '.segs*'):
                        if os.path.isdir(segments):
                            shutil.rmtree(segments)
                record['state'] = 'pending'
            record['source_hash'] = node.get('hash')
            record.update({'name': node['name'], 'size': node['size'], 'path': node['path'],
                           'local': record.get('local') or self.dest_path(node, dest_dir)})
            if record['state'] == 'done' and size_on_disk(record.get('local')) == node['size']:
                continue
            if record['state'] == 'unverified':
                if size_on_disk(record.get('local')):
                    # an hour of quota is not worth re-litigating an unproven rule on
                    # every pass, so the quarantined copy stays put until a human
                    # deletes it or --verify gets a different answer
                    continue
                # the quarantine was deleted: that is the ask to fetch it again
                record['state'] = 'pending'
                record['attempts'] = 0
                record['local'] = record.get('home') or record.get('local')
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
                local, how = self.fetch_to(restored, node, dest_dir, record)
                if how == 'unverified':
                    self.forget([restored], node['size'])
                    self.files_done += 1          # bytes landed; the verdict is separate
                    self.state.save()
                    continue
                record.update({'state': 'downloaded', 'local': local,
                               'seconds': round(time.time() - started, 1)})
                self.state.save()
                self.forget([restored], node['size'])
                record['state'] = 'done'
                record['error'] = None
                # a good copy is in place, so the bytes we could not vouch for are now
                # provably waste; drop them rather than leave a second copy forever
                stale = record.get('quarantined')
                if stale and os.path.exists(stale):
                    os.remove(stale)
                    self.log(f'  旧的未通过副本已清掉 {os.path.basename(stale)[:44]}')
                record['quarantined'] = None
                self.state.save()
                self.files_done += 1
                self.bytes_done += node['size']
                self.throttle_hits = 0      # the counter means *consecutive*
                self.log(f'    {how}，云端已清理 -> {os.path.basename(local)[:56]}')
            except (PikPakError, OSError) as error:
                if isinstance(error, TrafficCapped):
                    # the cap is not this file's fault: hand back the attempt just
                    # spent so the next run still has the full budget for it
                    record['attempts'] = max(record['attempts'] - 1, 0)
                    record['state'] = 'pending'
                else:
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
        # decided from the ids this listing produced, not from the url field on the
        # records: a key format change must never be able to hide unfinished work
        pending = [node_id for node_id in (item['id'] for item in files)
                   if self.state.data['files'].get(file_key(key, node_id), {}).get('state')
                   not in ('done', 'too_big')]
        link['status'] = 'done' if not pending and not report['truncated'] else 'incomplete'
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
        if self.args.dry_run:
            for job in jobs[:self.args.limit or len(jobs)]:
                report = self.inventory(job)
                self.log(f'{job["folder"]}: 计划 {len(report["files"])} 个文件 / '
                         f'{human(report["total"])}')
            return 0
        if self.args.inventory_only:
            self.quota_limit = self.space()['limit']
            return self.report(jobs)
        try:
            os.makedirs(self.args.dest, exist_ok=True)
        except OSError as error:
            self.log(f'目标目录建不出来：{self.args.dest}（{error}）', 'error')
            return 2
        if not os.access(self.args.dest, os.W_OK):
            self.log(f'目标目录不可写: {self.args.dest}', 'error')
            return 2
        self.state.reset_transient_failures()
        self.state.save()
        space = self.space()
        self.quota_limit = space['limit']
        self.log(f'{len(jobs)} 个链接 -> {self.args.dest} | 云盘 '
                 f'{human(space["usage"])}/{human(space["limit"])} 已用'
                 f'（回收站 {human(space["in_trash"])}）| '
                 f'本地可用 {human(shutil.disk_usage(self.args.dest).free)} | '
                 f'并发 {self.args.connections} 段')
        if self.args.limit:
            jobs = jobs[:self.args.limit]
        for pass_number in itertools.count(1):
            if pass_number > (self.args.repeat or 1):
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
            if tally.get('throttled'):
                return 4 if self.traffic_capped else 1
            if self.stop():
                return 1
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
        self.log('仍有链接未完成；重跑同一命令即可从断点继续', 'warn')
        return 1

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

    def doctor(self):
        """Preflight a machine before a long unattended run.

        Everything in here has broken a deployment somewhere: a missing `curl`, a FIPS
        build that refuses SHA-1, a destination volume that folds case, an expired
        session noticed only after the fourth hour. Costs no cloud space, and takes no
        lock, so it is safe to run while a download is in flight. Returns 1 when
        something has to be fixed first, which makes it usable from a cron wrapper."""
        try:
            import fcntl
        except ImportError:
            fcntl = None
        import hashlib

        rows = []

        def check(name, level, detail):
            rows.append((name, level))
            print(f'  {name:<11}{level:<6}{detail}')

        print('环境预检（不占云盘额度，可在下载进行中跑）')
        check('python', 'ok' if sys.version_info >= (3, 10) else 'FAIL',
              f'{sys.version.split()[0]}，需要 >= 3.10')
        try:
            hashlib.sha1(b'probe').hexdigest()
            check('sha1', 'ok', '内容核对可用')
        except ValueError as error:                       # FIPS builds
            check('sha1', 'FAIL', f'SHA-1 被解释器拒绝，内容核对不可用：{error}')
        if fcntl is None:
            check('平台', 'FAIL', '没有 fcntl，本工具不支持这个平台（Windows 请用 WSL）')
        else:
            check('平台', 'ok', 'fcntl 单实例锁可用')
        curl = shutil.which('curl')
        if self.args.connections > 1:
            check('curl', 'ok' if curl else 'FAIL',
                  (curl + '（分段下载要用它）') if curl
                  else '缺 curl，而 --connections > 1 需要它：装 curl 或用 --connections 1')
        else:
            check('curl', 'ok' if curl else 'note',
                  '单流用 urllib，不需要 curl' if not curl else curl)
        # The library holds bytes and progress; the account directory holds sessions.
        wanted, by_path = [], {}
        for label, path, needs_space in (('库目录', self.args.dest, True),
                                         ('进度目录', self.args.state_dir, False),
                                         ('账号目录', self.account_dir, False)):
            key = os.path.abspath(path)
            if key in by_path:
                by_path[key][0] += f'+{label}'
                continue
            row = [label, path, needs_space]
            by_path[key] = row
            wanted.append(row)
        for label, path, needs_space in wanted:
            if not os.path.isdir(path):
                # creating it is what a run would do anyway; doing it here turns
                # "no such path" into a reported failure instead of a traceback
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as error:
                    check(label, 'FAIL', f'{path} 建不出来：{error}')
                    continue
                check(label, 'ok', f'{path} 已创建')
            if not os.access(path, os.W_OK):
                check(label, 'FAIL', f'{path} 不可写')
                continue
            check(label, 'ok', f'{path} 可写')
            if needs_space:
                free = shutil.disk_usage(path).free
                check('剩余空间', 'ok' if free >= LOCAL_HEADROOM else 'FAIL',
                      f'{human(free)} 可用，{label}所在卷至少需要 {human(LOCAL_HEADROOM)}')
        records = self.state.data['files']
        done = [rec for rec in records.values() if rec.get('state') == 'done']
        check('已有进度', 'ok', f'{len(done)} 个完成记录 / {len(records)} 条')
        if self.case_insensitive():
            check('目的卷', 'note', '大小写不敏感：重名的不同大小写会改名让路（已按此处理）')
        if fcntl:
            try:
                handle = open(os.path.join(self.args.state_dir, 'lock'), 'a')
            except OSError as error:
                check('并发实例', 'warn', f'锁文件打不开：{error}')
            else:
                # append mode on purpose: 'w' would truncate the pid another instance
                # wrote there and make its own status line lie
                with handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        check('并发实例', 'warn', '已有实例持有锁：同一 state 目录不要并发跑')
                    else:
                        check('并发实例', 'ok', '没有其他实例持有锁（探测完已释放）')
        proxies = {key: value for key, value in os.environ.items() if 'proxy' in key.lower()}
        if proxies:
            check('出口', 'ok', '、'.join(f'{key}={value}' for key, value in sorted(proxies.items())))
        else:
            # observed: a login worked only after the user exported *_proxy by hand —
            # neither urllib nor curl reads a system or desktop proxy configuration
            check('出口', 'note', '无 *_proxy 环境变量（urllib 与 curl 都不读系统/桌面代理'
                              '设置，只读环境变量；被 PikPak 按地址拒绝时看这里）')
        session = self.client.session
        if not session.access_token:
            check('会话', 'FAIL', '还没有可用会话；先运行 --login')
            return self._doctor_verdict(rows)
        minutes = int(session.expires_in() // 60)
        check('会话', 'ok' if session.data.get('refresh_token') else 'warn',
              f'user {session.user_id}，access_token 还有 {minutes} 分钟'
              + ('' if session.data.get('refresh_token') else '，且没有 refresh_token（到点就得重登）'))
        try:
            space = self.client.space()
            slots = self.client.offline_task_slots()
            check('账号', 'ok', f'云盘 {human(space["usage"])}/{human(space["limit"])}'
                              f'（回收站 {human(space["in_trash"])}）'
                              f'，离线任务位 {slots["usage"]}/{slots["limit"]}')
        except PikPakError as error:
            check('账号', 'FAIL', f'API 走不通：{error}')
            return self._doctor_verdict(rows)
        try:
            folders = [item for item in self.client.list_folder('*')
                       if item.get('kind') == 'drive#folder'
                       and item.get('name') == DOT_DIR_NAME]
            if len(folders) > 1:
                check('临时目录', 'FAIL', f'网盘根目录有 {len(folders)} 个 {DOT_DIR_NAME}')
                return self._doctor_verdict(rows)
            leftovers = self.client.list_folder(folders[0]['id']) if folders else []
        except PikPakError as error:
            check('云端残留', 'warn', f'列不出来：{error}')
        else:
            size = sum(int(item.get('size') or 0) for item in leftovers)
            check('云端残留', 'warn' if leftovers else 'ok',
                  f'{DOT_DIR_NAME} 内 {len(leftovers)} 项、{human(size)}；下次实际下载启动前会清理'
                  if leftovers else f'{DOT_DIR_NAME} 内没有待清理内容')
        return self._doctor_verdict(rows)

    @staticmethod
    def _doctor_verdict(rows):
        failed = [name for name, level in rows if level == 'FAIL']
        warned = [name for name, level in rows if level == 'warn']
        print(f'结论：{len(failed)} 项必须先处理'
              + (f'（{", ".join(failed)}）' if failed else '')
              + (f'，{len(warned)} 项提醒' if warned else ''))
        return 1 if failed else 0

    def status(self):
        """Per-folder progress plus an ETA from the throughput this run actually
        got, not a theoretical one."""
        by_url = collections.defaultdict(list)
        for rec in self.state.data['files'].values():
            by_url[rec.get('url')].append(rec)
        links = sorted(self.state.data['links'].items(), key=lambda kv: kv[1].get('order') or 999)
        if links:
            # a header with no rows underneath reads as broken output, which is exactly
            # what it looks like when --dest names a library that has no journal yet
            print(f"{'folder':<26}{'status':<12}{'files':>7}{'done':>6}"
                  f"{'bytes':>11}{'planned':>11}  progress")
        totals = {'got': 0, 'planned': 0, 'spent': 0.0, 'skipped': 0}
        for url, info in links:
            records = by_url.get(url, [])
            planned = sum(rec['size'] for rec in records)
            got = sum(rec['size'] for rec in records if rec['state'] == 'done')
            spent = sum(rec.get('seconds') or 0 for rec in records if rec['state'] == 'done')
            skipped = sum(rec['size'] for rec in records if rec['state'] == 'too_big')
            over = sum(1 for rec in records if rec['state'] == 'too_big')
            done = sum(1 for rec in records if rec['state'] == 'done')
            share = (got / planned * 100) if planned else 0
            note = f'  ({over} over quota)' if over else ''
            unverified = sum(1 for rec in records if rec['state'] == 'unverified')
            if unverified:
                note += f'  ({unverified} unverified)'
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
        partial = sorted(_glob.glob(os.path.join(self.args.dest, '*', '*.part')))
        for path in partial:
            age = time.time() - os.path.getmtime(path)
            print(f'下载中: {os.path.basename(path)[:52]}… {human(os.path.getsize(path))}'
                  f'（{age:.0f}s 前还在写）')
        if not links and not partial:
            print(f'{self.args.state_dir} 里没有任何进度记录：这个库还没跑过，'
                  '或者 --dest 没指到那个库（进度跟着库走）')
        try:
            space = self.space()
            free = disk_free(self.args.dest)
            print(f'云盘 {human(space["usage"])}/{human(space["limit"])} 已用'
                  + (f'，本地剩余 {human(free)}' if free is not None else
                     f'，本地 {self.args.dest} 还没法测量'))
        except PikPakError as error:
            # a report that could not reach the account is not a clean report: a cron
            # wrapper has to be able to tell the two apart
            print(f'云盘配额查询失败: {error}')
            return 1
        return 0
