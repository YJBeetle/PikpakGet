"""Synthetic command-level account rotation test."""
import os
import tempfile
import unittest
from unittest import mock

import pikpakget.cli as cli
from pikpakget.pipeline import Log


class TestAccountRotation(unittest.TestCase):
    def test_different_series_have_independent_locks(self):
        root = tempfile.mkdtemp()
        args = cli.build_parser().parse_args(['--dest', root])
        cli.resolve_dirs(args)
        first = cli.series_args(args, 'A')
        second = cli.series_args(args, 'B')
        a = cli._acquire_lock(first.state_dir)
        b = cli._acquire_lock(second.state_dir)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        try:
            self.assertIsNone(cli._acquire_lock(first.state_dir))
            self.assertFalse(os.path.exists(os.path.join(root, '.pikpakget')))
        finally:
            a.close()
            b.close()

    def test_multiple_text_files_keep_input_order_and_series_journals(self):
        root = tempfile.mkdtemp()
        first = os.path.join(root, '嗷大喵.txt')
        second = os.path.join(root, 'xxx.txt')
        with open(first, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID1111111111\tA\n')
        with open(second, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID2222222222\tB\n')
            handle.write('https://mypikpak.com/s/EXAMPLEID3333333333\tA\n')
        args = cli.build_parser().parse_args([first, second, '--dest', os.path.join(root, 'dest'),
                                              '--log', '-'])
        cli.resolve_dirs(args)
        seen = []

        class Lock:
            def close(self):
                pass

        class Registry:
            def __init__(self, home):
                self.items = [{'id': 'one', 'label': 'one'}]

            def choose(self, excluded=(), only=None, respect_cooldown=False):
                return self.items[0], Lock()

            def client(self, item, logger=None, stop=None):
                return type('Client', (), {'prepare_workspace': lambda self: 'pack'})()

        class Pipeline:
            def __init__(self, args, log, **kwargs):
                self.args = args
                self.files_done = 0

            def run(self, jobs):
                seen.append((self.args.state_dir,
                             [(job['share_id'], job['folder'], job['order']) for job in jobs]))
                return 0

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, 'Pipeline', Pipeline), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock()), \
                mock.patch.object(cli, '_install_stop_handler'):
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
            args.limit = 2
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
        self.assertEqual(seen, [
            (os.path.join(args.dest, 'A', '.pikpakget'),
             [('EXAMPLEID1111111111', 'A', 1)]),
            (os.path.join(args.dest, 'B', '.pikpakget'),
             [('EXAMPLEID2222222222', 'B', 2)]),
            (os.path.join(args.dest, 'A', '.pikpakget'),
             [('EXAMPLEID3333333333', 'A', 3)]),
            (os.path.join(args.dest, 'A', '.pikpakget'),
             [('EXAMPLEID1111111111', 'A', 1)]),
            (os.path.join(args.dest, 'B', '.pikpakget'),
             [('EXAMPLEID2222222222', 'B', 2)])])

    def test_cloud_cleanup_defaults_to_no(self):
        items = [{'id': 'old', 'name': 'old.mp4'}]
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), \
                mock.patch('builtins.input') as answer:
            for value, expected in [('y', True), ('Y', True), ('', False),
                                    ('n', False), ('删除', False)]:
                answer.return_value = value
                self.assertEqual(cli._confirm_cloud_cleanup(
                    items, set(), 'account', lambda *args: None), expected)
            self.assertIn('[y/N]', answer.call_args.args[0])
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=False), \
                mock.patch('builtins.input', side_effect=AssertionError('must not prompt')):
            self.assertFalse(cli._confirm_cloud_cleanup(
                items, set(), 'account', lambda *args: None))

    def test_status_needs_no_account_or_account_lock(self):
        root = tempfile.mkdtemp()
        dest = os.path.join(root, 'lib')
        args = cli.build_parser().parse_args([os.path.join(root, 'links.txt'),
                                              '--dest', dest, '--status'])
        with mock.patch.dict(os.environ, {'PIKPAKGET_HOME': root}):
            cli.resolve_dirs(args)
        with mock.patch.object(cli, 'Accounts', side_effect=AssertionError('accounts loaded')), \
                mock.patch.object(cli, '_acquire_lock', side_effect=AssertionError('lock taken')), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
        self.assertTrue(any('没有任何进度记录' in str(call)
                            for call in printed.call_args_list))

    def test_all_accounts_locked_reports_unavailable(self):
        root = tempfile.mkdtemp()
        links = os.path.join(root, 'links.txt')
        with open(links, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID1111111111\n')
        args = cli.build_parser().parse_args([links, '--dest', os.path.join(root, 'lib')])
        with mock.patch.dict(os.environ, {'PIKPAKGET_HOME': root}):
            cli.resolve_dirs(args)
        released = []

        class Registry:
            def __init__(self, home):
                self.items = [{'id': 'first', 'label': 'first'}]

            def choose(self, excluded=(), only=None, respect_cooldown=False):
                return None, None

            def traffic_retry_at(self, item):
                return 0

        class Lock:
            def close(self):
                released.append(True)

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock()), \
                mock.patch.object(cli, '_install_stop_handler'), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(cli._commands(args, Log(quiet=True)), 3)
        self.assertTrue(any('没有可用账号' in str(call)
                            for call in printed.call_args_list))
        self.assertEqual(released, [True])

    def test_traffic_cap_releases_first_account_then_uses_second(self):
        root = tempfile.mkdtemp()
        links = os.path.join(root, 'links.txt')
        with open(links, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID1111111111\n')
        args = cli.build_parser().parse_args([links, '--dest', os.path.join(root, 'lib')])
        with mock.patch.dict(os.environ, {'PIKPAKGET_HOME': root}):
            cli.resolve_dirs(args)
        visited, released = [], []

        class Lock:
            def __init__(self, name):
                self.name = name

            def close(self):
                released.append(self.name)

        class Registry:
            def __init__(self, home):
                self.items = [{'id': 'first', 'label': 'first'},
                              {'id': 'second', 'label': 'second'}]
                self.capped = set()

            def choose(self, excluded=(), only=None, respect_cooldown=False):
                for item in self.items:
                    if item['id'] not in excluded and not (
                            respect_cooldown and item['id'] in self.capped):
                        return item, Lock(item['id'])
                return None, None

            def mark_traffic_capped(self, item):
                self.capped.add(item['id'])

            def traffic_retry_at(self, item):
                return cli.time.time() + 3600 if item['id'] in self.capped else 0

            def client(self, item, logger=None, stop=None):
                return type('FakeClient', (), {
                    'prepare_workspace': lambda self, **kwargs: 'folder'})()

        class Pipeline:
            def __init__(self, args, log, stop, client, account_id, workspace_id,
                         confirm_cleanup, account_label):
                self.account_id = account_id
                self.account_label = account_label
                self.files_done = 0

            def run(self, jobs):
                visited.append((self.account_id, self.account_label))
                return 4 if self.account_id == 'first' else 0

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, 'Pipeline', Pipeline), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock('library')), \
                mock.patch.object(cli, '_install_stop_handler'):
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
        self.assertEqual(visited, [('first', 'first'), ('second', 'second')])
        self.assertEqual(released, ['first', 'second', 'library'])

    def test_all_cooling_accounts_wait_then_retry_the_earliest(self):
        root = tempfile.mkdtemp()
        links = os.path.join(root, 'links.txt')
        with open(links, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID1111111111\n')
        args = cli.build_parser().parse_args([links, '--dest', os.path.join(root, 'lib')])
        with mock.patch.dict(os.environ, {'PIKPAKGET_HOME': root}):
            cli.resolve_dirs(args)
        clock = [1000]
        visited, released, deadlines = [], [], []

        class Lock:
            def __init__(self, name):
                self.name = name

            def close(self):
                released.append(self.name)

        class Registry:
            def __init__(self, home):
                self.items = [{'id': 'first', 'label': 'first'},
                              {'id': 'second', 'label': 'second'}]
                self.retry = {}

            def choose(self, excluded=(), only=None, respect_cooldown=False):
                for item in self.items:
                    if item['id'] in excluded or (respect_cooldown and
                            self.traffic_retry_at(item) > clock[0]):
                        continue
                    return item, Lock(item['id'])
                return None, None

            def traffic_retry_at(self, item):
                return self.retry.get(item['id'], 0)

            def mark_traffic_capped(self, item):
                self.retry[item['id']] = clock[0] + 3600

            def client(self, item, logger=None, stop=None):
                return type('Client', (), {'prepare_workspace': lambda self: 'pack'})()

        class Pipeline:
            def __init__(self, args, log, stop, client, account_id, workspace_id,
                         confirm_cleanup, account_label):
                self.account_id = account_id
                self.files_done = 0

            def run(self, jobs):
                visited.append(self.account_id)
                if len(visited) == 2:
                    clock[0] += 20
                return 4 if len(visited) <= 2 else 0

        def wait(deadline):
            self.assertEqual(released[-1], 'second')
            deadlines.append(deadline)
            clock[0] = deadline
            return True

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, 'Pipeline', Pipeline), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock('library')), \
                mock.patch.object(cli, '_install_stop_handler'), \
                mock.patch.object(cli, '_wait_for_traffic_retry', side_effect=wait), \
                mock.patch.object(cli.time, 'time', side_effect=lambda: clock[0]), \
                mock.patch.object(cli, 'STOP', False):
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
        self.assertEqual(visited, ['first', 'second', 'first'])
        self.assertEqual(deadlines, [4600])
        self.assertEqual(released, ['first', 'second', 'first', 'library'])

    def test_waiting_for_cooldown_stops_on_interrupt(self):
        with mock.patch.object(cli, 'STOP', False), \
                mock.patch.object(cli.time, 'time', return_value=1000), \
                mock.patch.object(cli.time, 'sleep',
                                  side_effect=lambda seconds: setattr(cli, 'STOP', True)) as sleep:
            self.assertFalse(cli._wait_for_traffic_retry(4600))
            sleep.assert_called_once_with(1.0)


class TestDirectUrlArgs(unittest.TestCase):
    def collect(self, argv):
        root = tempfile.mkdtemp()
        args = cli.build_parser().parse_args(argv + ['--dest', os.path.join(root, 'dest'),
                                                     '--log', '-'])
        cli.resolve_dirs(args)
        seen = []

        class Lock:
            def close(self):
                pass

        class Registry:
            def __init__(self, home):
                self.items = [{'id': 'one', 'label': 'one'}]

            def choose(self, excluded=(), only=None, respect_cooldown=False):
                return self.items[0], Lock()

            def client(self, item, logger=None, stop=None):
                return type('Client', (), {'prepare_workspace': lambda self: 'pack'})()

        class Pipeline:
            def __init__(self, args, log, **kwargs):
                self.files_done = 0

            def run(self, jobs):
                seen.extend((job['share_id'], job['folder'], job['order']) for job in jobs)
                return 0

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, 'Pipeline', Pipeline), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock()), \
                mock.patch.object(cli, '_install_stop_handler'):
            code = cli._commands(args, Log(quiet=True))
        return code, seen

    def test_urls_and_files_share_one_ordered_list(self):
        root = tempfile.mkdtemp()
        links = os.path.join(root, '系列.txt')
        with open(links, 'w', encoding='utf-8') as handle:
            handle.write('https://mypikpak.com/s/EXAMPLEID2222222222\tA\n')
        code, seen = self.collect([
            'https://mypikpak.com/s/EXAMPLEID1111111111', links,
            'https://mypikpak.com/s/EXAMPLEID3333333333\tB'])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [('EXAMPLEID1111111111', '未分组', 1),
                                ('EXAMPLEID2222222222', 'A', 2),
                                ('EXAMPLEID3333333333', 'B', 3)])

    def test_default_folder_overrides_the_fallback(self):
        code, seen = self.collect(['https://mypikpak.com/s/EXAMPLEID1111111111',
                                   '--default-folder', '合集'])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [('EXAMPLEID1111111111', '合集', 1)])

    def test_bad_url_is_a_problem_unless_yes(self):
        code, seen = self.collect(['https://example.com/s/EXAMPLEID1111111111'])
        self.assertEqual(code, 2)
        self.assertEqual(seen, [])
        code, seen = self.collect(['https://example.com/s/EXAMPLEID1111111111', '--skip-bad-lines'])
        self.assertEqual(code, 2)
        self.assertEqual(seen, [])
