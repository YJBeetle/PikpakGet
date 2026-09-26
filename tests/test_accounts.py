"""Synthetic account, lock and cloud workspace tests; no real PikPak calls."""
import os
import tempfile
import unittest
from unittest import mock

from pikpakget.accounts import Accounts, TRAFFIC_RETRY_SECONDS
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
        registry = Accounts(self.home)
        with open(registry.device_path(first['id']), encoding='utf-8') as handle:
            original_device = handle.read()
        used_ids = []

        def sign_in(client, username, password):
            used_ids.append(client.device_id)
            return {'access_token': 'access', 'refresh_token': 'refresh',
                    'sub': 'server-user-1', 'expires_at': 9999999999}

        with mock.patch.object(Client, 'sign_in', sign_in):
            second = Accounts(self.home).login('renamed@example.com',
                                               'synthetic-password')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(Accounts(self.home).items), 1)
        self.assertEqual(Accounts(self.home).items[0]['label'], 'renamed@example.com')
        self.assertEqual(len(used_ids), 2)
        self.assertEqual(used_ids[-1], original_device)
        with open(registry.device_path(second['id']), encoding='utf-8') as handle:
            self.assertEqual(handle.read(), original_device)

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

    def test_confirmed_traffic_cap_is_persistent_and_expires_after_an_hour(self):
        first = self.login('one@example.com', 'user-1')
        second = self.login('two@example.com', 'user-2')
        registry = Accounts(self.home)
        with mock.patch('pikpakget.accounts.time.time', return_value=1000):
            lock = registry.acquire(first)
            try:
                registry.mark_traffic_capped(first)
            finally:
                lock.close()
        path = os.path.join(registry.directory(first['id']), 'traffic_capped_at')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        fresh = Accounts(self.home)
        self.assertEqual(fresh.traffic_retry_at(first), 1000 + TRAFFIC_RETRY_SECONDS)
        with mock.patch('pikpakget.accounts.time.time', return_value=4599):
            selected, lock = fresh.choose(respect_cooldown=True)
            self.assertEqual(selected['id'], second['id'])
            lock.close()
            selected, lock = fresh.choose()
            self.assertEqual(selected['id'], first['id'])  # read-only commands can use it
            lock.close()
        with mock.patch('pikpakget.accounts.time.time', return_value=4600):
            selected, lock = fresh.choose(respect_cooldown=True)
            self.assertEqual(selected['id'], first['id'])
            lock.close()

    def test_a_second_confirmed_cap_moves_the_retry_time_forward(self):
        item = self.login('one@example.com', 'user-1')
        registry = Accounts(self.home)
        for now in (1000, 4600):
            with mock.patch('pikpakget.accounts.time.time', return_value=now):
                lock = registry.acquire(item)
                try:
                    registry.mark_traffic_capped(item)
                finally:
                    lock.close()
        self.assertEqual(Accounts(self.home).traffic_retry_at(item), 8200)

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
        self.assertNotEqual(registry.client(first).device_id,
                            registry.client(second).device_id)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'device_id')))
        for item in (first, second):
            path = registry.client(item).session.path
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(registry.device_path(item['id'])).st_mode & 0o777,
                             0o600)

    def test_missing_account_device_requires_relogin(self):
        item = self.login('one@example.com', 'user-1')
        registry = Accounts(self.home)
        os.remove(registry.device_path(item['id']))
        with self.assertRaisesRegex(PikPakError, '重新运行 --login'):
            registry.client(item)
        self.assertFalse(os.path.exists(registry.device_path(item['id'])))
        self.login('one@example.com', 'user-1')
        self.assertTrue(os.path.isfile(registry.device_path(item['id'])))

    def test_logout_removes_device_id(self):
        item = self.login('one@example.com', 'user-1')
        registry = Accounts(self.home)
        registry.logout(item)
        self.assertFalse(os.path.exists(registry.device_path(item['id'])))
        self.assertFalse(os.path.exists(os.path.join(registry.directory(item['id']),
                                                      'session.json')))


class TestCloudWorkspace(unittest.TestCase):
    def test_restore_does_not_retry_an_ambiguous_post(self):
        client = Client.__new__(Client)
        calls = []
        client.call = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {}
        client.restore('share', 'token', ['file'], 'pack')
        self.assertEqual(calls[0][2]['retries'], 1)
        self.assertEqual(calls[0][2]['json_body']['parent_id'], 'pack')

    def test_root_requests_use_the_api_root_identifier(self):
        client = Client.__new__(Client)
        calls = []

        def call(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {'file': {'id': 'workspace'}}

        client.call = call
        client._paginate = lambda path, params: calls.append(('GET', path, params)) or iter(())
        client.list_folder('*')
        client.create_folder('.pikpakget')
        client.list_folder('workspace')
        client.create_folder('child', 'workspace')
        self.assertNotIn('parent_id', calls[0][2])
        self.assertEqual(calls[1][2]['json_body']['parent_id'], '')
        self.assertEqual(calls[2][2]['parent_id'], 'workspace')
        self.assertEqual(calls[3][2]['json_body']['parent_id'], 'workspace')

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

    def test_creates_restore_folder_without_clearing_its_children(self):
        self.entries['workspace'] = [{'id': 'old-file'}, {'id': 'old-subfolder'}]
        self.assertEqual(self.client.prepare_workspace(), 'workspace')
        self.assertEqual(self.deleted, [])

    def test_refuses_ambiguous_same_name_folders(self):
        self.entries['*'] = [
            {'id': 'a', 'name': 'Pack From Shared', 'kind': 'drive#folder'},
            {'id': 'b', 'name': 'Pack From Shared', 'kind': 'drive#folder'}]
        with self.assertRaises(PikPakError):
            self.client.prepare_workspace()
        self.assertEqual(self.deleted, [])
