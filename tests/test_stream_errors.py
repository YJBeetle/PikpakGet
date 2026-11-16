"""Only connection and response-read failures qualify as network errors."""
import errno
import io
import tempfile
import unittest
import urllib.error
from unittest import mock

from pikpakget.api import PikPakError
from pikpakget.stream import download_stream


class TestStreamErrors(unittest.TestCase):
    def response(self, error=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.side_effect = error or [b'payload', b'']
        return response

    def test_open_disk_error_is_not_a_network_failure(self):
        failure = OSError(errno.ENOSPC, 'disk full')
        response = self.response()
        with mock.patch('urllib.request.urlopen', return_value=response), \
                mock.patch('builtins.open', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                download_stream('https://example.invalid/file', 'target')
        self.assertIs(caught.exception, failure)
        response.__exit__.assert_called_once()

    def test_write_and_flush_errors_keep_their_original_exception(self):
        for operation in ('write', '__exit__'):
            with self.subTest(operation=operation):
                failure = OSError(errno.EIO, 'disk I/O failure')
                handle = mock.MagicMock()
                handle.__enter__.return_value = handle
                getattr(handle, operation).side_effect = failure
                with mock.patch('urllib.request.urlopen', return_value=self.response()), \
                        mock.patch('builtins.open', return_value=handle):
                    with self.assertRaises(OSError) as caught:
                        download_stream('https://example.invalid/file', 'target')
                self.assertIs(caught.exception, failure)

    def test_connection_failure_is_still_marked_network(self):
        failure = urllib.error.URLError('connection reset')
        with mock.patch('urllib.request.urlopen', side_effect=failure):
            with self.assertRaises(PikPakError) as caught:
                download_stream('https://example.invalid/file', 'target')
        self.assertTrue(caught.exception.network)
        self.assertIs(caught.exception.__cause__, failure)

    def test_read_failure_is_network_and_retains_downloaded_prefix(self):
        failure = ConnectionResetError('connection reset')
        response = self.response()
        response.read.side_effect = [b'prefix', failure]
        with tempfile.TemporaryDirectory() as root, \
                mock.patch('urllib.request.urlopen', return_value=response):
            path = root + '/file.part'
            with self.assertRaises(PikPakError) as caught:
                download_stream('https://example.invalid/file', path)
            self.assertTrue(caught.exception.network)
            with open(path, 'rb') as handle:
                self.assertEqual(handle.read(), b'prefix')

    def test_http_refusal_keeps_its_status_and_is_not_network(self):
        failure = urllib.error.HTTPError('https://example.invalid/file', 403,
                                         'Forbidden', {}, io.BytesIO())
        with mock.patch('urllib.request.urlopen', side_effect=failure):
            with self.assertRaises(PikPakError) as caught:
                download_stream('https://example.invalid/file', 'target')
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(caught.exception.network)
        failure.close()
