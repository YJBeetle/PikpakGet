"""Shared classification, bounded preflight retries and mutation safety."""
import http.client
import os
import tempfile
import time
import unittest
from unittest import mock

from pikpakget.api import Client
from pikpakget.errors import (PikPakError, TrafficCapped, MutationOutcomeUnknown,
                              RestoreOutcomeUnknown)
from pikpakget.network import REQUEST_ATTEMPTS, retry


class TestRequestPolicy(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.client = Client(session_path=os.path.join(root.name, 'session.json'))
        self.client.session.store({'access_token': 'access', 'refresh_token': 'refresh',
                                   'sub': 'user', 'expires_at': time.time() + 7200})
        self.client._wait = mock.Mock()

    def test_one_tls_failure_is_retried_in_every_preflight_and_api_path(self):
        cases = [
            ('refresh', lambda c: c.refresh(), {'access_token': 'new', 'refresh_token': 'refresh'}),
            ('captcha', lambda c: c.captcha_token('GET:/drive/v1/share'), {'captcha_token': 'shield'}),
            ('api', lambda c: c.about(), {'quota': {}}),
        ]
        for kind, operation, success in cases:
            with self.subTest(kind=kind):
                self.client._wait.reset_mock()
                self.client._captchas.clear()
                if kind == 'api':
                    self.client._captchas['GET:/drive/v1/about'] = ('shield', time.time() + 7200)
                self.client._raw = mock.Mock(side_effect=[
                    (None, {'error_description': 'SSL: UNEXPECTED_EOF_WHILE_READING'}),
                    (200, success)])
                operation(self.client)
                self.assertEqual(self.client._raw.call_count, 2)
                self.client._wait.assert_called_once_with(5)
                self.assertFalse(self.client.session_dead)

    def test_exhausted_captcha_does_not_multiply_outer_budget_or_submit_restore(self):
        self.client._raw = mock.Mock(return_value=(None, {'error_description': 'TLS EOF'}))
        with self.assertRaises(PikPakError) as caught:
            self.client.restore('share', 'token', ['file'], 'pack')
        self.assertNotIsInstance(caught.exception, MutationOutcomeUnknown)
        self.assertTrue(caught.exception.network)
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertEqual(self.client._raw.call_count, REQUEST_ATTEMPTS)
        self.assertTrue(all('/captcha/init' in call.args[1]
                            for call in self.client._raw.call_args_list))

    def test_exhausted_refresh_does_not_multiply_captcha_and_api_budgets(self):
        self.client.session.data['expires_at'] = 0
        self.client._raw = mock.Mock(return_value=(None, {'error_description': 'Connection refused'}))
        with self.assertRaises(PikPakError) as caught:
            self.client.share_info('share')
        self.assertTrue(caught.exception.network)
        self.assertEqual(self.client._raw.call_count, REQUEST_ATTEMPTS)
        self.assertTrue(all(call.args[1].endswith('/auth/token')
                            for call in self.client._raw.call_args_list))
        self.assertFalse(self.client.session_dead)

    def test_restore_preflight_recovers_then_submits_only_once(self):
        self.client._raw = mock.Mock(side_effect=[
            (None, {'error_description': 'timeout'}),
            (200, {'captcha_token': 'shield'}),
            (200, {'restore_status': 'OK'})])
        self.assertEqual(self.client.restore('share', 'token', ['file'], 'pack'),
                         {'restore_status': 'OK'})
        calls = self.client._raw.call_args_list
        self.assertEqual(sum(call.args[1].endswith('/share/restore') for call in calls), 1)

    def test_uncertain_writes_are_not_replayed_even_for_server_errors(self):
        for status in (None, 503):
            for path, error_type in [('/v1/share/restore', RestoreOutcomeUnknown),
                                     ('/v1/files', MutationOutcomeUnknown)]:
                with self.subTest(status=status, path=path):
                    self.client._wait.reset_mock()
                    self.client._captchas[f'POST:/drive{path}'] = ('shield', time.time() + 7200)
                    self.client._raw = mock.Mock(return_value=(status, {'error_description': 'outage'}))
                    with self.assertRaises(error_type):
                        self.client.call('POST', path)
                    self.assertEqual(self.client._raw.call_count, 1)
                    self.client._wait.assert_not_called()

    def test_idempotent_cleanup_can_recover_from_a_dropped_reply(self):
        self.client._captchas['POST:/drive/v1/files:batchTrash'] = ('shield', time.time() + 7200)
        self.client._raw = mock.Mock(side_effect=[
            (None, {'error_description': 'TLS EOF'}), (200, {})])
        self.client.batch_trash(['file'])
        self.assertEqual(self.client._raw.call_count, 2)

    def test_http_body_read_failure_does_not_become_invalid_credentials(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 401
        response.read.side_effect = http.client.IncompleteRead(b'partial', 5)
        with mock.patch('urllib.request.urlopen', return_value=response):
            status, body = self.client._raw('GET', 'https://example.invalid/')
        self.assertIsNone(status)
        self.assertIn('IncompleteRead', body['error_description'])

    def test_permanent_refusals_and_traffic_caps_never_back_off(self):
        for error in (PikPakError('revoked', status=403), TrafficCapped('daily limit', status=400)):
            with self.subTest(error=type(error).__name__):
                operation = mock.Mock(side_effect=error)
                wait = mock.Mock()
                with self.assertRaises(type(error)):
                    retry(operation, log=lambda *a: None, stop=lambda: False, wait=wait)
                self.assertEqual(operation.call_count, 1)
                wait.assert_not_called()

    def test_service_outage_is_not_reported_as_account_rate_limit(self):
        error = self.client._response_error(503, {'error_description': 'unavailable'}, 'read')
        self.assertTrue(error.transient)
        self.assertFalse(error.throttled)
        error = self.client._response_error(403, {'error_description': 'failed to generate token'}, 'read')
        self.assertFalse(error.throttled, 'generate contains rate, but is not a rate-limit signal')
