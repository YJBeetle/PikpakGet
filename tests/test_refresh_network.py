"""An unavailable auth endpoint is not evidence that credentials expired."""
import os
import tempfile
import time
import unittest
from unittest import mock

from pikpakget.api import Client, NETWORK_RETRY_WAITS, PikPakError
from pikpakget.cli import build_parser
from pikpakget.pipeline import Pipeline


class TestRefreshNetwork(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.messages = []
        self.client = Client(session_path=os.path.join(root.name, 'session.json'),
                             device_id_path=os.path.join(root.name, 'device_id'),
                             logger=lambda message, level='info': self.messages.append(message))
        self.client.session.store({'access_token': 'old', 'refresh_token': 'refresh',
                                   'sub': 'user', 'expires_at': time.time() - 1})
        self.client._wait = mock.Mock()

    def test_refused_connection_recovers_without_relogin(self):
        self.client._raw = mock.Mock(side_effect=[
            (None, {'error_description': 'Connection refused'}),
            (200, {'access_token': 'new', 'refresh_token': 'rotated', 'expires_in': 3600})])
        self.assertEqual(self.client.ensure_session(), 'new')
        self.assertFalse(self.client.session_dead)
        self.assertEqual(self.client.session.data['refresh_token'], 'rotated')
        self.client._wait.assert_called_once_with(NETWORK_RETRY_WAITS[0])
        self.assertFalse(any('重新登录' in message for message in self.messages))

    def test_persistent_outage_preserves_tokens_and_marks_error_network(self):
        original = dict(self.client.session.data)
        self.client._raw = mock.Mock(return_value=(None, {'error_description': 'Connection refused'}))
        with self.assertRaises(PikPakError) as caught:
            self.client.refresh()
        self.assertTrue(caught.exception.network)
        self.assertFalse(self.client.session_dead)
        self.assertNotIn('重新登录', str(caught.exception))
        self.assertEqual(self.client.session.data, original)
        self.assertEqual(self.client._raw.call_count, len(NETWORK_RETRY_WAITS))
        self.assertEqual([c.args[0] for c in self.client._wait.call_args_list],
                         list(NETWORK_RETRY_WAITS[:-1]))

    def test_temporary_server_failure_and_rate_limit_do_not_kill_session(self):
        self.client._raw = mock.Mock(side_effect=[
            (503, {'error_description': 'unavailable'}),
            (429, {'error_description': 'too many requests'}),
            (200, {'access_token': 'new', 'refresh_token': 'refresh'})])
        self.client.refresh()
        self.assertFalse(self.client.session_dead)
        self.assertEqual(self.client._raw.call_count, 3)

    def test_invalid_refresh_token_requires_login_without_retry(self):
        self.client._raw = mock.Mock(return_value=(401, {'error_description': 'invalid refresh token'}))
        with self.assertRaisesRegex(PikPakError, '请重新登录') as caught:
            self.client.refresh()
        self.assertFalse(caught.exception.network)
        self.assertTrue(self.client.session_dead)
        self.client._wait.assert_not_called()

    def test_stop_during_retry_preserves_session(self):
        stopped = [False]
        self.client.stop = lambda: stopped[0]
        self.client._raw = mock.Mock(return_value=(None, {'error_description': 'Connection refused'}))
        self.client._wait.side_effect = lambda seconds: stopped.__setitem__(0, True)
        with self.assertRaisesRegex(PikPakError, '已中断'):
            self.client.refresh()
        self.assertEqual(self.client._raw.call_count, 1)
        self.assertFalse(self.client.session_dead)


class TestRoundQuotaOutage(unittest.TestCase):
    def test_round_summary_network_failure_does_not_cancel_next_pass(self):
        with tempfile.TemporaryDirectory() as root:
            args = build_parser().parse_args(['--dest', root, '--repeat', '2'])
            args.state_dir = os.path.join(root, '.state')
            pipeline = Pipeline(args, lambda *a: None)
            failure = PikPakError('Connection refused', network=True, action='refresh')
            pipeline.space = mock.Mock(side_effect=[
                {'limit': 10000, 'usage': 0, 'in_trash': 0}, failure, failure])
            passes = []
            job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111',
                   'folder': 'Series', 'order': 1}

            def run_link(item):
                passes.append(item)
                pipeline.state.link(item['url'])['status'] = 'done' if len(passes) == 2 else 'partial'
                pipeline.files_done += 1
                return 'ok' if len(passes) == 2 else 'retry'

            pipeline.run_link = run_link
            self.assertEqual(pipeline.run([job]), 0)
            self.assertEqual(len(passes), 2)
