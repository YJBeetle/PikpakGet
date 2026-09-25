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
    """Where `state.json`, the lock and the logs of one library live.

    The remembered library keeps its journal in `~/.pikpakget`, exactly as it always
    did: pointing `--status` at `~/Downloads` should not create that directory just to
    ask a question. Any *other* library carries its own journal, because that is the
    case where the volume is the shared thing — a disk two machines mount has to answer
    "what have I already downloaded here" the same way from both."""
    return (account_dir() if os.path.abspath(dest) == os.path.abspath(remembered)
            else os.path.join(os.path.abspath(dest), DOT_DIR_NAME))


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
    # the remembered library is by definition the one whose journal stays in home
    print(f'不带 --dest 就跑这个库，进度记在 {account_dir()}')
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
    parser.add_argument('--connections', type=int, default=4,
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
    parser.add_argument('--dry-run', action='store_true', help='plan only; no writes, no deletes')
    parser.add_argument('--no-delete', action='store_true',
                        help='keep the cloud copies after downloading (fills the quota fast)')
    parser.add_argument('--no-sweep', action='store_true',
                        help='do not reclaim the cloud copies an earlier interrupted run '
                             'left behind at startup (they otherwise occupy quota)')
    parser.add_argument('--purge-trash', action='store_true',
                        help='allow emptying the whole trash when the quota is the blocker')
    parser.add_argument('--yes', action='store_true', help='ignore unparsable link lines')
    parser.add_argument('--log', default='auto', help="write a log file into the progress "
                                                     "directory (default), or '-' to log "
                                                     "to stdout only")
    parser.add_argument('--version', action='store_true', help='print version and exit')
    parser.add_argument('--quiet', action='store_true')
    return parser


def _warn_about_legacy_state(args, log):
    """Point at a login or a journal this run is not reading.

    The two have different homes now — the session always belongs to the account
    directory, the journal to whichever library is in use — so each is compared against
    its own home rather than one list of candidate directories. The repo-local
    `.pikpakget/` the cwd-relative default used to write is searched for both, because
    that is where every one of these files used to live."""
    repo = os.path.join(os.path.abspath(os.getcwd()), DOT_DIR_NAME)
    library = os.path.join(os.path.abspath(args.dest), DOT_DIR_NAME)
    journal = os.path.abspath(args.state_dir)
    login = os.path.abspath(args.account_dir)
    for name, what, here, there in (
            ('state.json', '下载进度', journal, repo),
            ('state.json', '下载进度', journal, library),
            ('state.json', '下载进度', journal, login),
            ('session.json', '登录会话', login, repo),
            ('session.json', '登录会话', login, library)):
        if here == there or os.path.exists(os.path.join(here, name)):
            continue                    # the same directory, or ours already has one
        found = os.path.join(there, name)
        if os.path.exists(found):
            log(f'注意到 {found} 里有{what}，本次读的是 {os.path.join(here, name)}；'
                '要认那份就把它挪过去，或者把 --dest 指回那个库', 'warn')


def _acquire_lock(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    handle = open(os.path.join(state_dir, 'grab.lock'), 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
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
    # a preflight that dies on the very problem it is meant to report is useless, so
    # --doctor neither creates the state directory nor writes a log into it
    try:
        os.makedirs(args.state_dir, exist_ok=True)
    except OSError as error:
        if not args.doctor:
            print(f'进度目录不可用：{args.state_dir}（{error}）', file=sys.stderr)
            return 2
    # an unattended run outlives the terminal, so the same lines that scroll past
    # are kept on disk (stdout stays the primary output)
    log = (Log(quiet=args.quiet) if args.doctor or args.log == '-'
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
    _warn_about_legacy_state(args, log)
    try:
        # the login and the device id belong to the account, so they stay in home even
        # when this library carries its journal elsewhere: a second --dest must not mean
        # a second sign-in, and must not move the credential onto a mounted volume
        client = Client(session_path=os.path.join(args.account_dir, 'session.json'),
                        device_id_path=os.path.join(args.account_dir, 'device_id'), logger=log)
    except OSError as error:
        # the device id is written into the account directory, so an unusable one fails
        # here rather than in whatever call happens to need it first — and a device id
        # that silently became random would change how the account looks to PikPak
        print(f'账号目录不可用：{args.account_dir}（{error}）', file=sys.stderr)
        return 1 if args.doctor else 2
    # every command below can fail for reasons that are the user's to act on — no
    # session, a refused login, a blocked account — and a Python traceback says
    # nothing more useful than the one line we can print; only a real bug earns one
    try:
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
            client.sign_in(account, password)
            return 0
        if args.logout:
            client.session.forget()
            log(f'已删除本地会话（{os.path.join(args.account_dir, "session.json")}）')
            return 0
        pipeline = Pipeline(args, log, stop=lambda: STOP)
        if args.doctor:
            return pipeline.doctor()
        if args.status_only:
            return pipeline.status()
        if args.whoami:
            about = client.about()
            space = client.space()
            expires = str(about.get('expires_at') or '').strip()
            trash = f'，回收站占 {human(space["in_trash"])}' if space['in_trash'] else ''
            print(f'云盘   {human(space["usage"])} / {human(space["limit"])}{trash}')
            print(f'订阅   {expires.split("T")[0] + " 到期" if expires else "无到期时间"}')
            return 0
    except PikPakError as error:
        print(f'错误：{error}', file=sys.stderr)
        return 2
    if not args.links:
        build_parser().error('a links file is required (or use --login/--whoami/--status)')

    lock = _acquire_lock(args.state_dir)
    if lock is None:
        print('另一个实例正在运行（state 目录下的 grab.lock 被占用），先停掉它')
        return 3
    jobs, problems = read_links(args.links, load_folder_map(args.folder_map),
                               args.default_folder)
    for problem in problems:
        log(problem, 'error')
    if problems and not args.yes:
        log('链接文件有无法解析的行，先修好再跑（或加 --yes 跳过这些行）', 'error')
        return 2
    if not jobs:
        log('链接文件里没有可用链接', 'error')
        return 2
    _install_stop_handler()
    if args.verify_only:
        return pipeline.verify(jobs)
    if args.dry_run:
        log('dry-run：不会转存、下载或删除')
    try:
        return pipeline.run(jobs)
    except PikPakError as error:
        log(f'中止：{error}', 'error')
        return 1
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
