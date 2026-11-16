"""Account listing inspects local locks and cooldowns without logging in."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from pikpakget.accounts import Accounts
from pikpakget import cli
from pikpakget.api import PikPakError


class TestAccountStatus(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.home = root.name
        self.registry = Accounts(self.home)
        self.registry.items = [
            {'id': 'first', 'label': 'one@example.com'},
            {'id': 'second', 'label': 'two@example.com'},
            {'id': 'third', 'label': 'three@example.com'}]
        with open(self.registry.index, 'w', encoding='utf-8') as handle:
            json.dump(self.registry.items, handle)

    def capped_at(self, item, stamp):
        directory = self.registry.directory(item['id'])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, 'traffic_capped_at'), 'w') as handle:
            handle.write(str(stamp))

    def test_lock_probe_preserves_pid_and_does_not_take_lease(self):
        item = self.registry.items[0]
        lease = self.registry.acquire(item)
        self.addCleanup(lease.close)
        path = os.path.join(self.registry.directory(item['id']), 'lock')
        with open(path) as handle:
            original = handle.read()
        self.assertTrue(self.registry.is_in_use(item))
        with open(path) as handle:
            self.assertEqual(handle.read(), original)
        lease.close()
        self.assertFalse(self.registry.is_in_use(item), 'a stale PID is not a live lock')
        with open(path) as handle:
            self.assertEqual(handle.read(), original)
        again = self.registry.acquire(item)
        self.assertIsNotNone(again, 'the status probe must release its lock')
        again.close()

    def test_missing_lock_does_not_create_account_directory(self):
        item = self.registry.items[0]
        self.assertFalse(self.registry.is_in_use(item))
        self.assertFalse(os.path.exists(self.registry.root))

    def test_listing_shows_usage_and_cooldowns_without_client_or_lease(self):
        args = cli.build_parser().parse_args(['--accounts'])
        args.account_dir = self.home
        first, second, third = self.registry.items
        self.capped_at(first, 1000)  # retry at 4600; current time is 4500
        self.capped_at(second, 500)  # retry at 4100, already elapsed
        lease = self.registry.acquire(first)
        self.addCleanup(lease.close)
        output = io.StringIO()
        with mock.patch.object(Accounts, 'client', side_effect=AssertionError('network client')), \
                mock.patch.object(Accounts, 'acquire', side_effect=AssertionError('lease')), \
                mock.patch.object(cli.time, 'time', return_value=4500), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli._commands(args, lambda *a: None), 0)
        rows = output.getvalue().splitlines()
        self.assertIn(first['label'], rows[0])
        self.assertIn('本机：使用中', rows[0])
        self.assertIn('冷却中，剩余 1分40秒', rows[0])
        self.assertIn('后可重试', rows[0])
        self.assertIn(second['label'], rows[1])
        self.assertIn('空闲', rows[1])
        self.assertIn('冷却已到期', rows[1])
        self.assertIn(third['label'], rows[2])
        self.assertIn('未记录限流', rows[2])
        self.assertFalse(os.path.exists(self.registry.directory(third['id'])))

    def test_unreadable_lock_is_not_reported_as_idle(self):
        with mock.patch('builtins.open', side_effect=PermissionError('no access')):
            with self.assertRaisesRegex(PikPakError, '占用状态无法读取'):
                self.registry.is_in_use(self.registry.items[0])

    def test_bad_cooldown_does_not_hide_other_accounts(self):
        self.capped_at(self.registry.items[0], 'broken')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli._show_accounts(self.registry), 1)
        self.assertIn('流量：未知', output.getvalue())
        self.assertIn('three@example.com', output.getvalue())

    def test_no_accounts_prints_login_hint_without_creating_files(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(output):
            self.assertEqual(cli._show_accounts(Accounts(root)), 0)
            self.assertEqual(os.listdir(root), [])
        self.assertIn('先运行 --login', output.getvalue())
