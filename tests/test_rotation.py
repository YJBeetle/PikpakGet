"""Synthetic command-level account rotation test."""
import os
import tempfile
import unittest
from unittest import mock

import pikpakget.cli as cli
from pikpakget.pipeline import Log


class TestAccountRotation(unittest.TestCase):
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

            def client(self, item, logger=None):
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

            def client(self, item, logger=None):
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
