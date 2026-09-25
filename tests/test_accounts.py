"""Synthetic account, lock and cloud workspace tests; no real PikPak calls."""
import os
import tempfile
import unittest
from unittest import mock

from pikpakget.accounts import Accounts
from pikpakget.api import Client, PikPakError


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def login(self, name, user_id):
        with mock.patch.object(Client, 'sign_in', return_value={
                'access_token': 'access', 'refresh_token': 'refresh',
                'sub': user_id, 'expires_at': 9999999999}):
            return Accounts(self.home).login(name, 'synthetic-password')

    def test_same_server_user_updates_one_session(self):
        first = self.login('first@example.com', 'server-user-1')
        second = self.login('renamed@example.com', 'server-user-1')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(Accounts(self.home).items), 1)
        self.assertEqual(Accounts(self.home).items[0]['label'], 'renamed@example.com')

    def test_locked_account_is_skipped_and_all_locked_is_reportable(self):
        first = self.login('one@example.com', 'user-1')
        second = self.login('two@example.com', 'user-2')
        registry = Accounts(self.home)
        first_lock = registry.acquire(first)
        try:
            selected, second_lock = registry.choose()
            try:
                self.assertEqual(selected['id'], second['id'])
                self.assertIsNotNone(second_lock)
                self.assertEqual(registry.choose(), (None, None))
            finally:
                second_lock.close()
        finally:
            first_lock.close()

    def test_relogin_checks_known_account_lock_before_sign_in(self):
        item = self.login('one@example.com', 'user-1')
        registry = Accounts(self.home)
        lock = registry.acquire(item)
        try:
            with mock.patch.object(Client, 'sign_in') as sign_in:
                with self.assertRaisesRegex(PikPakError, '正在下载'):
                    registry.login('one@example.com', 'synthetic-password')
                sign_in.assert_not_called()
        finally:
            lock.close()

    def test_sessions_are_separate_and_private(self):
        first = self.login('one@example.com', 'user-1')
        second = self.login('two@example.com', 'user-2')
        registry = Accounts(self.home)
        self.assertNotEqual(registry.client(first).session.path,
                            registry.client(second).session.path)
        for item in (first, second):
            path = registry.client(item).session.path
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class TestCloudWorkspace(unittest.TestCase):
    def setUp(self):
        self.client = Client.__new__(Client)
        self.deleted = []
        self.entries = {'*': [], 'workspace': []}
        self.client.list_folder = lambda parent='*': list(self.entries[parent])
        self.client.create_folder = lambda name: {'id': 'workspace'}
        self.client.cleanup = self.cleanup

    def cleanup(self, ids):
        self.deleted.extend(ids)
        self.entries['workspace'] = []

    def test_creates_dedicated_folder_and_clears_its_children(self):
        self.entries['workspace'] = [{'id': 'old-file'}, {'id': 'old-subfolder'}]
        self.assertEqual(self.client.prepare_workspace(), 'workspace')
        self.assertEqual(self.deleted, ['old-file', 'old-subfolder'])

    def test_refuses_ambiguous_same_name_folders(self):
        self.entries['*'] = [
            {'id': 'a', 'name': '.pikpakget', 'kind': 'drive#folder'},
            {'id': 'b', 'name': '.pikpakget', 'kind': 'drive#folder'}]
        with self.assertRaises(PikPakError):
            self.client.prepare_workspace()
        self.assertEqual(self.deleted, [])
