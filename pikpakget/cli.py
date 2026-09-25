"""Command line entry point."""
import argparse
import getpass
import json
import os
import signal
import sys
import time

from . import __version__
from .api import (DEFAULT_DEST, DOT_DIR_NAME, Client, PikPakError,
                 account_dir)
from .accounts import Accounts
from .pipeline import Log, Pipeline, human, load_folder_map, read_links

try:
    import fcntl
except ImportError:                                # Windows: no POSIX locks
    fcntl = None

STOP = False


def _install_stop_handler():
    def handler(signum, frame):                                # noqa: ARG001
        global STOP
        if STOP:
            print('再次收到中断，立即退出（状态已保存）')
            os._exit(1)
        STOP = True
        print('\n收到中断：处理完当前文件后退出，进度已保存，重跑即续传', flush=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)



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


def state_dir_for(dest, remembered):
    """Every library owns its journal and lock, including the remembered library."""
    return os.path.join(os.path.abspath(dest), DOT_DIR_NAME)


def resolve_dirs(args):
    """Fix the three anchors once: dest, the journal beside it, and the account folder.

    `read_config` warns as it goes, so it is called once here rather than per derived
    value: a broken config file would otherwise say so two or three times."""
    remembered = remembered_dest()
    args.dest = os.path.abspath(os.path.expanduser(args.dest or remembered))
    args.account_dir = account_dir()
    args.state_dir = state_dir_for(args.dest, remembered)


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
    print(f'不带 --dest 就跑这个库，进度记在 {os.path.join(stored.get("dest", default_dest()), DOT_DIR_NAME)}')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='pikpakget',
        description='Sequentially grab PikPak share links, one file at a time, '
                    'working around a small cloud quota.')
    parser.add_argument('links', nargs='?', help='text file, one share link per line '
                                                 '(optionally "<url>\\t<folder>")')
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
    parser.add_argument('--default-folder', default='(unfiled)',
                        help='folder name for lines without one (default: %(default)s)')
    parser.add_argument('--connections', type=int, default=1,
                        help='ranged connections per file. Use 1 for a plain single '
                             'stream. On a free account extra lanes are often starved; '
                             'if one is refused outright the run drops to 1 connection '
                             'and stays there')
    parser.add_argument('--gap', type=float, default=20,
                        help='seconds to rest between files, to keep request density low')
    parser.add_argument('--repeat', type=int, default=0,
                        help='pass over the list up to N times, returning to links that '
                             'had a transient failure (0 = one pass). Stops early if a '
                             'whole pass lands nothing')
    parser.add_argument('--limit', type=int, default=0, help='process at most N links')
    parser.add_argument('--max-files', type=int, default=0, help='download at most N files')
    parser.add_argument('--inventory', dest='inventory_only', action='store_true',
                        help='size up each link (file count, bytes, largest file) without '
                             'using any cloud space')
    parser.add_argument('--inventory-out', default='inventory.csv')
    parser.add_argument('--verify', dest='verify_only', action='store_true',
                        help='re-check every file already marked downloaded against the '
                             'content hash the share reports; deletes nothing, exits 1 on '
                             'a mismatch')
    parser.add_argument('--dry-run', action='store_true',
                        help='plan only; no restore, download or cloud cleanup')
    parser.add_argument('--no-delete', action='store_true',
                        help='keep the cloud copies after downloading (fills the quota fast)')
    parser.add_argument('--purge-trash', action='store_true',
                        help='allow emptying the whole trash when the quota is the blocker')
    parser.add_argument('--yes', action='store_true', help='ignore unparsable link lines')
    parser.add_argument('--log', default='auto', help="write a log file into the progress "
                                                     "directory (default), or '-' to log "
                                                     "to stdout only")
    parser.add_argument('--version', action='store_true', help='print version and exit')
    parser.add_argument('--quiet', action='store_true')
    return parser


def _acquire_lock(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    handle = open(os.path.join(state_dir, 'lock'), 'a+')
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
    readonly = (args.doctor or args.status_only or args.whoami or args.accounts
                or args.login is not None or args.logout or args.dry_run or args.inventory_only)
    if not readonly:
        try:
            os.makedirs(args.state_dir, exist_ok=True)
        except OSError as error:
            print(f'进度目录不可用：{args.state_dir}（{error}）', file=sys.stderr)
            return 2
    # an unattended run outlives the terminal, so the same lines that scroll past
    # are kept on disk (stdout stays the primary output)
    log = (Log(quiet=args.quiet) if readonly or args.log == '-'
           else Log(os.path.join(args.state_dir, f'grab-{time.strftime("%Y-%m-%d")}.log'),
                    args.quiet))
    # the log is opened here, so it has to be closed here: every early return used to
    # leave the handle to the garbage collector, which shows up as an unclosed-file
    # warning at exit and keeps the file locked on Windows
    try:
        return _commands(args, log)
    finally:
        log.close()


def _commands(args, log):
    try:
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
            jobs, problems = read_links(args.links, load_folder_map(args.folder_map),
                                       args.default_folder)
            for problem in problems:
                log(problem, 'error')
            if problems and not args.yes:
                return 2
            if not jobs:
                raise PikPakError('链接文件里没有可用链接')
        elif not (args.doctor or args.status_only or args.whoami):
            build_parser().error('a links file is required (or use --login/--whoami/--status)')
        writable_job = jobs is not None and not (args.dry_run or args.inventory_only)
        library_lock = _acquire_lock(args.state_dir) if writable_job else None
        if writable_job and library_lock is None:
            print(f'本地库正在被另一个进程使用：{args.state_dir}/lock')
            return 3
        try:
            if jobs:
                _install_stop_handler()
            excluded = set()
            remaining_files = args.max_files
            while True:
                item, account_lock = registry.choose(excluded, only=args.account)
                if account_lock is None:
                    if not registry.items:
                        raise PikPakError('没有已登录账号；先运行 --login')
                    candidates = [entry for entry in registry.items
                                  if entry['id'] not in excluded and
                                  (not args.account or entry['id'] == registry.find(args.account)['id'])]
                    if not candidates:
                        log('所有可用账号均已触及流量限制或初始化失败', 'error')
                        return 1
                    print('没有可用账号：所有账号都被其他进程锁定')
                    return 3
                try:
                    client = registry.client(item, logger=log)
                    actual_download = jobs and not (args.verify_only or args.dry_run
                                                     or args.inventory_only)
                    if actual_download:
                        try:
                            workspace_id = client.prepare_workspace()
                        except PikPakError as error:
                            log(f'账号 {item["label"]} 的网盘临时文件夹无法清理：{error}', 'error')
                            excluded.add(item['id'])
                            continue
                    else:
                        workspace_id = None
                    if remaining_files:
                        args.max_files = remaining_files
                    pipeline = Pipeline(args, log, stop=lambda: STOP, client=client,
                                        account_id=item['id'], workspace_id=workspace_id)
                    if args.doctor:
                        return pipeline.doctor()
                    if args.status_only:
                        return pipeline.status()
                    if args.whoami:
                        about = client.about()
                        space = client.space()
                        expires = str(about.get('expires_at') or '').strip()
                        print(f'账号   {item["label"]}')
                        print(f'云盘   {human(space["usage"])} / {human(space["limit"])}')
                        print(f'订阅   {expires.split("T")[0] + " 到期" if expires else "无到期时间"}')
                        return 0
                    if args.verify_only:
                        return pipeline.verify(jobs)
                    code = pipeline.run(jobs)
                    if code == 4:
                        if remaining_files:
                            remaining_files -= pipeline.files_done
                            if remaining_files <= 0:
                                return 1
                        excluded.add(item['id'])
                        log(f'账号 {item["label"]} 下行流量已满，尝试下一个账号', 'warn')
                        continue
                    return code
                finally:
                    account_lock.close()
        finally:
            if library_lock:
                library_lock.close()
    except PikPakError as error:
        print(f'错误：{error}', file=sys.stderr)
        return 2
    except OSError as error:
        print(f'本地文件操作失败：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
