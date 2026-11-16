"""Command line entry point."""
import argparse
import copy
import getpass
import json
import math
import os
import signal
import sys
import time

from . import __version__
from .api import (DEFAULT_DEST, DOT_DIR_NAME, Client, PikPakError,
                 account_dir)
from .accounts import Accounts
from .pipeline import (DEFAULT_LINK_FOLDER, Log, Pipeline, human, load_folder_map,
                       parse_link_line, read_links)

try:
    import fcntl
except ImportError:                                # Windows: no POSIX locks
    fcntl = None

STOP = False


def _confirm_cloud_cleanup(items, owned_ids, label, log):
    log(f'账号 {label} 的 Pack From Shared 中有 {len(items)} 项可清理：', 'warn')
    for item in items[:10]:
        owner = '本系列记录' if item['id'] in owned_ids else '归属未知'
        log(f'  {item.get("name") or "(无名称)"} ({owner}，ID: {item["id"]})', 'warn')
    if len(items) > 10:
        log(f'  另有 {len(items) - 10} 项未列出', 'warn')
    if not sys.stdin.isatty():
        log('当前没有交互终端，未清理；需要清理时请在交互式终端重跑', 'warn')
        return False
    try:
        return input('永久删除上述 Pack From Shared 项目及其内容？[y/N] ').strip().lower() == 'y'
    except (EOFError, KeyboardInterrupt):
        return False


def _install_stop_handler():
    def handler(signum, frame):                                # noqa: ARG001
        global STOP
        if STOP:
            print('再次收到中断，立即退出（状态已保存）')
            os._exit(1)
        STOP = True
        print('\n收到中断：正在停止，已完成进度和当前下载片段会保留，重跑即续传', flush=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def _wait_for_traffic_retry(deadline):
    """Wait without holding an account lease; Ctrl+C ends the wait promptly."""
    while not STOP:
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))
    return False



def default_dest():
    return os.path.expanduser(DEFAULT_DEST)


def read_config():
    """The remembered settings, or nothing usable.

    A file that cannot be read must not stop a run: it is consulted before a log file
    even exists, so a problem goes to stderr as one line and the built-in default takes
    over. The corrupt-state journal next door has behaved this way since it was written."""
    path = os.path.join(account_dir(), 'config.json')
    try:
        with open(path, encoding='utf-8') as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as error:
        print(f'{path} 读不了（{error}），本次按内置默认', file=sys.stderr)
        return {}
    if not isinstance(loaded, dict):
        print(f'{path} 不是一个对象，本次按内置默认', file=sys.stderr)
        return {}
    for key in sorted(set(loaded) - {'dest'}):
        print(f'{path} 里的 {key} 这一版不认识，已忽略（可写的只有 dest）', file=sys.stderr)
    return {key: value for key, value in loaded.items()
            if key == 'dest' and isinstance(value, str) and value.strip()}


def remembered_dest():
    return read_config().get('dest') or default_dest()


def series_args(args, folder):
    selected = copy.copy(args)
    selected.series_root = os.path.join(args.dest, folder)
    selected.state_dir = os.path.join(selected.series_root, DOT_DIR_NAME)
    return selected


def resolve_dirs(args):
    """Fix the download root and account folder once.

    `read_config` warns as it goes, so it is called once here rather than per derived
    value: a broken config file would otherwise say so two or three times."""
    remembered = remembered_dest()
    args.dest = os.path.abspath(os.path.expanduser(args.dest or remembered))
    args.account_dir = account_dir()
    args.state_dir = None
    args.series_root = None


def write_config(pairs):
    """`--set-config dest=/Volumes/nas/PikPak`, repeatable; `dest=` forgets it again.

    The file is rewritten from what we understood, so a hand-edited key that means
    nothing here is dropped with a warning instead of surviving to confuse the next
    read. Only `dest` is storable on purpose: the other flags are things you tune per
    run, and a stale value in a file nobody remembers writing is the hardest kind of
    difference to explain."""
    path = os.path.join(account_dir(), 'config.json')
    try:
        with open(path, encoding='utf-8') as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        stored = {}
    except (ValueError, OSError) as error:
        backup = f'{path}.corrupt-{time.strftime("%Y%m%d-%H%M%S")}'
        print(f'{path} 读不了（{error}），已备份到 {backup}，本次重新写一份', file=sys.stderr)
        try:
            os.replace(path, backup)
        except OSError:
            pass
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep:
            print(f'--set-config 需要 KEY=VALUE，收到的是 {pair}', file=sys.stderr)
            return 2
        key = key.strip().replace('-', '_')
        if key != 'dest':
            print(f'{key} 不能写进配置：这一版只记 dest，其余参数每次跑用命令行传',
                  file=sys.stderr)
            return 2
        stored[key] = value.strip()
    for key in list(stored):
        if key != 'dest' or not stored[key]:
            stored.pop(key)
        else:
            stored[key] = os.path.abspath(os.path.expanduser(stored[key]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(stored, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    print(f'已写入 {path}：'
          + (f'dest={stored["dest"]}' if stored else '已清空，回到 ~/Downloads/PikPak'))
    print('不带 --dest 就用这个下载根目录；每个分组的进度保存在分组内的 .pikpakget/')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='pikpakget',
        description='Sequentially grab PikPak share links, one file at a time, '
                    'working around a small cloud quota.')
    parser.add_argument('links', nargs='*', help='text files in processing order, '
                                                 'one share link per line '
                                                 '(optionally "<url>\\t<folder>"), '
                                                 'or share URLs directly')
    parser.add_argument('--login', metavar='USERNAME', nargs='?', const='',
                        help='sign in and store a '
                                                            'refreshable session')
    parser.add_argument('--password-stdin', action='store_true',
                        help='read the password from stdin instead of a prompt')
    parser.add_argument('--logout', action='store_true', help='delete the stored session')
    parser.add_argument('--accounts', action='store_true', help='list saved accounts in rotation order')
    parser.add_argument('--account', help='use one account by label or ID; with --logout, remove it')
    parser.add_argument('--set-config', action='append', metavar='KEY=VALUE',
                        help='remember a setting in ~/.pikpakget/config.json and exit '
                             '(the only key is dest; `--set-config dest=` forgets it)')
    parser.add_argument('--whoami', action='store_true',
                        help='show cloud space usage and subscription expiry')
    parser.add_argument('--doctor', action='store_true',
                        help='preflight this machine for a long run (python, curl, FIPS '
                             'SHA-1, volumes, session, quota) and exit 1 on anything that '
                             'has to be fixed first; costs no cloud space')
    parser.add_argument('--status', dest='status_only', action='store_true',
                        help='show per-folder progress, measured speed and ETA')
    parser.add_argument('--dest', default=None,
                        help='root folder; each link lands in <dest>/<folder>/ '
                             '(default: the one in ~/.pikpakget/config.json, '
                             'otherwise ~/Downloads/PikPak)')
    parser.add_argument('--folder-map', help='optional CSV/TSV of "url,folder" used when '
                                             'a links file line has no folder column')
    parser.add_argument('--default-folder', default=None,
                        help='folder name for unmapped lines without one '
                             '(default: each input file name without its '
                             'extension; direct URLs fall back to 未分组)')
    parser.add_argument('--connections', type=int, default=1,
                        help='ranged connections per file. Use 1 for a plain single '
                             'stream. On a free account extra lanes are often starved; '
                             'if one is refused outright the run drops to 1 connection '
                             'and stays there')
    parser.add_argument('--gap', type=float, default=20,
                        help='seconds to rest between files, to keep request density low')
    parser.add_argument('--repeat', type=int, default=3,
                        help='pass over the list up to N times, returning to links that '
                             'had a transient failure (default: %(default)s; 0 = one pass). Stops early if a '
                             'whole pass lands nothing')
    parser.add_argument('--limit', type=int, default=0, help='process at most N links')
    parser.add_argument('--max-files', type=int, default=0, help='download at most N files')
    parser.add_argument('--skip-ext', default='', metavar='EXT[,EXT...]',
                        help='skip files with these extensions, case-insensitive '
                             '(e.g. txt,nfo); skipped files are not restored, '
                             'downloaded or counted, in plans and audits alike')
    parser.add_argument('--inventory', dest='inventory_only', action='store_true',
                        help='size up each link (file count, bytes, largest file) without '
                             'using any cloud space')
    parser.add_argument('--inventory-out', default='inventory.csv')
    parser.add_argument('--verify', action='store_true',
                        help='hash-check existing files during download; with --dry-run, '
                             'only scan and report, without downloading or changing files')
    parser.add_argument('--dry-run', action='store_true',
                        help='plan only; no restore, download or cloud cleanup')
    parser.add_argument('--no-delete', action='store_true',
                        help='keep the cloud copies after downloading (fills the quota fast)')
    parser.add_argument('--skip-bad-lines', action='store_true',
                        help='ignore unparsable link lines')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='auto-confirm cloud cleanup without prompting; only '
                             'leftovers owned by this series are deleted, unknown '
                             'items are kept')
    parser.add_argument('--log', default='auto', help="write a log file into each series' "
                                                     "progress directory (default), or '-' "
                                                     "to log to stdout only")
    parser.add_argument('--version', action='store_true', help='print version and exit')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='show debug-level stage detail (restore, verify, cleanup)')
    return parser


def _acquire_lock(state_dir):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    if os.path.islink(state_dir):
        raise PikPakError(f'进度目录不能是符号链接：{state_dir}')
    descriptor = os.open(os.path.join(state_dir, 'lock'), os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, 'r+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _show_status(args, log):
    if not os.path.isdir(args.dest):
        print(f'{args.dest} 下没有任何进度记录')
        return 0
    roots = []
    for name in sorted(os.listdir(args.dest)):
        root = os.path.join(args.dest, name)
        if (os.path.isdir(root) and not os.path.islink(root)
                and os.path.isfile(os.path.join(root, DOT_DIR_NAME, 'state.json'))):
            roots.append(name)
    if not roots:
        print(f'{args.dest} 下没有任何进度记录')
        return 0
    for name in roots:
        print(f'系列：{name}')
        Pipeline(series_args(args, name), log).status()
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.set_config:
        return write_config(args.set_config)
    resolve_dirs(args)
    if fcntl is None:
        # refuse rather than start a multi-day job with no second-instance guard, or
        # worse: an unsynchronised one sharing its state file with another writer
        print('单实例锁依赖 POSIX 的 fcntl，Windows 上不支持：请在 WSL 或 Linux/macOS 里运行',
              file=sys.stderr)
        return 2
    log = Log(quiet=args.quiet, verbose=args.verbose)
    try:
        return _commands(args, log)
    finally:
        log.close()


def _commands(args, log):
    try:
        if args.status_only:
            return _show_status(args, log)
        registry = Accounts(args.account_dir)
        if args.doctor and not registry.items:
            client = Client(session_path=None, logger=log)
            return Pipeline(args, log, client=client).doctor()
        if args.login is not None:
            account = args.login
            if not account:
                # `--login` on its own is what people type; argparse's usage dump when
                # the value is missing is not an answer, but a piped or scripted call
                # has nobody to answer a prompt, so that case stays an error
                if sys.stdin.isatty():
                    account = input('PikPak 账号（邮箱/手机号/用户名）: ').strip()
                if not account:
                    print('--login 需要账号：--login <邮箱>，或在交互式终端里直接运行',
                          file=sys.stderr)
                    return 2
            password = (sys.stdin.read().strip() if args.password_stdin
                        else getpass.getpass('PikPak 密码: '))
            item = registry.login(account, password, logger=log)
            log(f'已保存账号 {item["label"]}（ID {item["id"]}）')
            return 0
        if args.accounts:
            for index, item in enumerate(registry.items, 1):
                print(f'{index}. {item["label"]}  {item["id"]}')
            if not registry.items:
                print('尚无账号；先运行 --login')
            return 0
        if args.logout:
            if not registry.items:
                raise PikPakError('没有已登录账号')
            if not args.account and len(registry.items) != 1:
                raise PikPakError('有多个账号时，--logout 需要同时指定 --account')
            item = registry.find(args.account) if args.account else registry.items[0]
            registry.logout(item)
            log(f'已删除账号 {item["label"]} 的本地会话')
            return 0
        jobs = None
        if args.links:
            jobs, problems = [], []
            folder_map = load_folder_map(args.folder_map)
            for path in args.links:
                if '://' in path:
                    job, problem = parse_link_line(
                        path, folder_map, args.default_folder or DEFAULT_LINK_FOLDER)
                    batch = [job] if job else []
                    issues = [f'命令行: {problem}'] if problem else []
                else:
                    batch, issues = read_links(path, folder_map, args.default_folder)
                for job in batch:
                    job['order'] = len(jobs) + 1
                    jobs.append(job)
                problems.extend(issues)
            for problem in problems:
                log(problem, 'error')
            if problems and not args.skip_bad_lines:
                return 2
            if not jobs:
                raise PikPakError('没有可用链接')
        elif not (args.doctor or args.status_only or args.whoami):
            build_parser().error('one or more links files are required '
                                 '(or use --login/--whoami/--status)')
        if jobs:
            _install_stop_handler()
            jobs = jobs[:args.limit or len(jobs)]
        grouped = []
        if jobs and not args.inventory_only and (not args.dry_run or args.verify):
            for job in jobs:
                if grouped and grouped[-1][0] == job['folder']:
                    grouped[-1][1].append(job)
                else:
                    grouped.append((job['folder'], [job]))
        else:
            grouped.append((None, jobs))
        remaining_files = args.max_files
        result = 0
        for folder, group in grouped:
            selected = series_args(args, folder) if folder else copy.copy(args)
            selected.limit = 0
            if args.max_files:
                selected.max_files = remaining_files
            writable = bool(group and not (args.dry_run or args.inventory_only))
            lock = None
            series_log = log
            if writable:
                if os.path.lexists(selected.series_root) and (
                        os.path.islink(selected.series_root)
                        or not os.path.isdir(selected.series_root)):
                    raise PikPakError(f'系列目录不是普通文件夹：{selected.series_root}')
                lock = _acquire_lock(selected.state_dir)
                if lock is None:
                    log(f'系列正在被另一个进程使用：{selected.state_dir}/lock', 'warn')
                    result = 3
                    continue
                if args.log != '-':
                    series_log = Log(os.path.join(selected.state_dir,
                                     f'grab-{time.strftime("%Y-%m-%d")}.log'),
                                     args.quiet, verbose=args.verbose)
            try:
                code, done = _run_group(selected, series_log, registry, group)
            finally:
                if series_log is not log:
                    series_log.close()
                if lock:
                    lock.close()
            if args.max_files:
                remaining_files -= done
            if code:
                result = code
            if STOP or (args.max_files and remaining_files <= 0):
                break
        return result
    except PikPakError as error:
        print(f'错误：{error}', file=sys.stderr)
        return 2
    except OSError as error:
        print(f'本地文件操作失败：{error}', file=sys.stderr)
        return 2


def _run_group(args, log, registry, jobs):
    excluded = set()
    completed = 0
    file_budget = args.max_files
    actual_download = bool(jobs and not (args.dry_run or args.inventory_only))
    while True:
        if STOP:
            log('已中断账号选择；进度已保存', 'warn')
            return 1, completed
        item, account_lock = registry.choose(
            excluded, only=args.account, respect_cooldown=actual_download)
        if account_lock is None:
            if not registry.items:
                raise PikPakError('没有已登录账号；先运行 --login')
            candidates = [entry for entry in registry.items
                          if entry['id'] not in excluded and
                          (not args.account or entry['id'] == registry.find(args.account)['id'])]
            if not candidates:
                log('所有候选账号均初始化失败', 'error')
                return 1, completed
            if actual_download:
                now = time.time()
                cooling = [(registry.traffic_retry_at(entry), entry) for entry in candidates]
                cooling = [(deadline, entry) for deadline, entry in cooling if deadline > now]
                if cooling:
                    deadline, next_item = min(cooling, key=lambda pair: pair[0])
                    log(f'暂无可下载账号；账号 {next_item["label"]} '
                        f'约 {math.ceil((deadline - now) / 60)} 分钟后重试'
                        f'（{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(deadline))}）',
                        'warn')
                    if not _wait_for_traffic_retry(deadline):
                        log('等待账号流量恢复时已中断；进度已保存', 'warn')
                        return 1, completed
                    continue
            print('没有可用账号：所有账号都被其他进程锁定')
            return 3, completed
        try:
            client = registry.client(item, logger=log, stop=lambda: STOP)
            if actual_download:
                try:
                    workspace_id = client.prepare_workspace()
                except PikPakError as error:
                    log(f'账号 {item["label"]} 的网盘临时文件夹无法清理：{error}', 'error')
                    excluded.add(item['id'])
                    continue
            else:
                workspace_id = None
            if file_budget:
                args.max_files = file_budget - completed
            pipeline = Pipeline(args, log, stop=lambda: STOP, client=client,
                                account_id=item['id'], workspace_id=workspace_id,
                                account_label=item['label'],
                                confirm_cleanup=lambda items, owned: _confirm_cloud_cleanup(
                                    items, owned, item['label'], log))
            if args.doctor:
                return pipeline.doctor(), completed
            if args.whoami:
                about = client.about()
                space = client.space()
                expires = str(about.get('expires_at') or '').strip()
                print(f'账号   {item["label"]}')
                print(f'云盘   {human(space["usage"])} / {human(space["limit"])}')
                print(f'订阅   {expires.split("T")[0] + " 到期" if expires else "无到期时间"}')
                return 0, completed
            code = pipeline.run(jobs)
            completed += pipeline.files_done
            if code == 4:
                registry.mark_traffic_capped(item)
                if file_budget and completed >= file_budget:
                    return 1, completed
                log(f'账号 {item["label"]} 下行流量已满，1 小时后再试；'
                    '现在尝试下一个账号', 'warn')
                continue
            return code, completed
        finally:
            account_lock.close()


if __name__ == '__main__':
    sys.exit(main())
