"""Segment writers must finish before reuse, and disk failures must escape."""
import errno
import io
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from pikpakget import stream


class TestSegmentWriters(unittest.TestCase):
    def test_slow_writer_finishes_before_segment_is_inspected_or_merged(self):
        release = threading.Event()
        joining = threading.Event()
        finished = threading.Event()
        timeouts, errors = [], []
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'file.part')

            def spawn(item, url):
                def write():
                    release.wait()
                    with open(item['path'], 'ab') as handle:
                        handle.write(b'12345678')
                writer = threading.Thread(target=write)
                writer.start()

                def join(timeout=None):
                    timeouts.append(timeout)
                    joining.set()
                    writer.join(timeout)

                return types.SimpleNamespace(
                    poll=lambda: 0, wait=lambda **kw: 0,
                    sink=types.SimpleNamespace(join=join), sink_error=None)

            def download():
                try:
                    stream.download_segments(lambda: 'unused', target, 8, 1,
                                             wait=lambda seconds: None)
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            with mock.patch.object(stream, '_spawn', side_effect=spawn) as spawned, \
                    mock.patch.object(stream, 'probe_status', side_effect=AssertionError('probe')):
                task = threading.Thread(target=download)
                task.start()
                try:
                    self.assertTrue(joining.wait(5))
                    self.assertFalse(finished.is_set())
                    self.assertFalse(os.path.exists(target))
                finally:
                    release.set()
                    task.join(5)
                self.assertFalse(task.is_alive())
                self.assertEqual(timeouts, [None])
                self.assertEqual(errors, [])
                self.assertEqual(spawned.call_count, 1)
            with open(target, 'rb') as handle:
                self.assertEqual(handle.read(), b'12345678')

    def test_drain_preserves_disk_error_even_when_curl_already_exited(self):
        failure = OSError(errno.ENOSPC, 'disk full')
        process = types.SimpleNamespace(
            stdout=io.BytesIO(b'payload'), sink_error=None,
            terminate=mock.Mock(side_effect=ProcessLookupError()))
        with mock.patch('builtins.open', side_effect=failure):
            stream._drain(process, 'segment')
        self.assertIs(process.sink_error, failure)
        self.assertTrue(process.stdout.closed)

    def test_disk_failure_joins_all_writers_and_skips_network_retry(self):
        failure = OSError(errno.EIO, 'disk write failed')
        joined = []

        def spawn(item, url):
            process = types.SimpleNamespace(
                poll=lambda: 0, wait=lambda **kw: 0,
                sink_error=failure if item['index'] == 0 else None,
                sink=types.SimpleNamespace(join=lambda: joined.append(item['index'])))
            return process

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(stream, '_spawn', side_effect=spawn) as spawned, \
                mock.patch.object(stream, 'probe_status', side_effect=AssertionError('probe')):
            target = os.path.join(root, 'file.part')
            with self.assertRaises(OSError) as caught:
                stream.download_segments(lambda: 'unused', target, 8, 2,
                                         wait=lambda seconds: None)
            self.assertIs(caught.exception, failure)
            self.assertEqual(joined, [0, 1])
            self.assertEqual(spawned.call_count, 2)
            self.assertFalse(os.path.exists(target))

    def test_close_failure_is_reported(self):
        failure = OSError(errno.EIO, 'flush failed')
        process = types.SimpleNamespace(stdout=io.BytesIO(b'x'), sink_error=None,
                                        terminate=mock.Mock())
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.__exit__.side_effect = failure
        with mock.patch('builtins.open', return_value=handle):
            stream._drain(process, 'segment')
        self.assertIs(process.sink_error, failure)

    def test_error_after_partial_write_is_preserved(self):
        failure = OSError(errno.ENOSPC, 'disk full during write')
        process = types.SimpleNamespace(stdout=io.BytesIO(b'payload'), sink_error=None,
                                        terminate=mock.Mock())

        def copy(source, target, length):
            target.write(b'pay')
            raise failure

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(stream.shutil, 'copyfileobj', side_effect=copy):
            path = os.path.join(root, 'segment')
            stream._drain(process, path)
            self.assertIs(process.sink_error, failure)
            with open(path, 'rb') as handle:
                self.assertEqual(handle.read(), b'pay')

    def test_reap_handles_sink_terminating_curl_concurrently(self):
        process = types.SimpleNamespace(poll=lambda: None,
                                        terminate=mock.Mock(side_effect=ProcessLookupError()),
                                        wait=mock.Mock())
        stream.reap([process])
        process.wait.assert_called_once_with(timeout=15)


if __name__ == '__main__':
    unittest.main()
