"""Synthetic command-level account rotation test."""
import os
import tempfile
import unittest
from unittest import mock

import pikpakget.cli as cli
from pikpakget.pipeline import Log


class TestAccountRotation(unittest.TestCase):
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

            def choose(self, excluded=(), only=None):
                return None, None

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

            def choose(self, excluded=(), only=None):
                for item in self.items:
                    if item['id'] not in excluded:
                        return item, Lock(item['id'])
                return None, None

            def client(self, item, logger=None):
                return type('FakeClient', (), {'prepare_workspace': lambda self: 'folder'})()

        class Pipeline:
            def __init__(self, args, log, stop, client, account_id, workspace_id):
                self.account_id = account_id
                self.files_done = 0

            def run(self, jobs):
                visited.append(self.account_id)
                return 4 if self.account_id == 'first' else 0

        with mock.patch.object(cli, 'Accounts', Registry), \
                mock.patch.object(cli, 'Pipeline', Pipeline), \
                mock.patch.object(cli, '_acquire_lock', return_value=Lock('library')), \
                mock.patch.object(cli, '_install_stop_handler'):
            self.assertEqual(cli._commands(args, Log(quiet=True)), 0)
        self.assertEqual(visited, ['first', 'second'])
        self.assertEqual(released, ['first', 'second', 'library'])
