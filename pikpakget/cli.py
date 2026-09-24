"""Command line entry point."""
import argparse
import fcntl
import getpass
import os
import signal
import sys

from .api import PikPakError, Client
from .pipeline import Log, Pipeline, load_folder_map, read_links

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


def build_parser():
    parser = argparse.ArgumentParser(
        prog='pikpakget',
        description='Sequentially grab PikPak share links, one file at a time, '
                    'working around a small cloud quota.')
    parser.add_argument('links', nargs='?', help='text file, one share link per line '
                                                 '(optionally "<url>\\t<folder>")')
    parser.add_argument('--login', metavar='USERNAME', help='sign in and store a '
                                                            'refreshable session')
    parser.add_argument('--password-stdin', action='store_true',
                        help='read the password from stdin instead of a prompt')
    parser.add_argument('--logout', action='store_true', help='delete the stored session')
    parser.add_argument('--whoami', action='store_true', help='show identity and quota')
    parser.add_argument('--status', dest='status_only', action='store_true',
                        help='show per-folder progress, measured speed and ETA')
    parser.add_argument('--dest', default=os.path.join(os.getcwd(), 'downloads'),
                        help='root folder; each link lands in <dest>/<folder>/')
    parser.add_argument('--state-dir', default=os.path.join(os.getcwd(), '.pikpakget'),
                        help='session, device id, state and lock live here')
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
    parser.add_argument('--limit', type=int, default=0, help='process at most N links')
    parser.add_argument('--max-files', type=int, default=0, help='download at most N files')
    parser.add_argument('--inventory', dest='inventory_only', action='store_true',
                        help='size up each link (file count, bytes, largest file) without '
                             'using any cloud space')
    parser.add_argument('--inventory-out', default='inventory.csv')
    parser.add_argument('--dry-run', action='store_true', help='plan only; no writes, no deletes')
    parser.add_argument('--no-delete', action='store_true',
                        help='keep the cloud copies after downloading (fills the quota fast)')
    parser.add_argument('--purge-trash', action='store_true',
                        help='allow emptying the whole trash when the quota is the blocker')
    parser.add_argument('--yes', action='store_true', help='ignore unparsable link lines')
    parser.add_argument('--quiet', action='store_true')
    return parser


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
    log = Log(quiet=args.quiet)
    client = Client(session_path=os.path.join(args.state_dir, 'session.json'),
                    device_id_path=os.path.join(args.state_dir, 'device_id'), logger=log)
    if args.login:
        password = (sys.stdin.read().strip() if args.password_stdin
                    else getpass.getpass('PikPak password: '))
        client.sign_in(args.login, password)
        return 0
    if args.logout:
        client.session.forget()
        log('已删除本地会话')
        return 0

    pipeline = Pipeline(args, log, stop=lambda: STOP)
    if args.status_only:
        return pipeline.status()
    if args.whoami:
        about = client.about()
        space = client.space()
        slots = client.offline_task_slots()
        print(f'user_type={about.get("user_type")} 云盘 {space["usage"]}/{space["limit"]} '
              f'（回收站 {space["in_trash"]}）离线任务位 {slots["usage"]}/{slots["limit"]}')
        return 0
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
