"""Transfer-path tests against a local Range-capable HTTP server, with real `curl`
and real bytes.

The rest of the suite is pure logic: `download_segments` was only ever exercised with
fake processes, which is how a segment written past its own range could be truncated,
trusted, and land as a file of the right length and a shifted middle. These tests
download for real, and every one of them failed before that bug was fixed.

They are skipped where curl is absent, since the segmented path is the only thing that
needs it (`--connections 1` goes over urllib).
"""
import hashlib
import http.server
import os
import shutil
import socketserver
import tempfile
import threading
import unittest

from pikpakget import stream

SIZE = 5 << 20                      # big enough to span four segments and a tail
PIECE = 1 << 20
DATA = bytes(range(256)) * (SIZE // 256)


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self._request()
        self.send_response(206 if self.headers.get('Range') else 200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request(self):
        start, end = 0, len(DATA) - 1
        header = self.headers.get('Range')
        if header:
            head, _, tail = header.replace('bytes=', '').partition('-')
            start = int(head)
            end = int(tail) if tail else len(DATA) - 1
        return DATA[start:end + 1]

    def log_message(self, *args):
        pass


@unittest.skipIf(shutil.which('curl') is None, 'the segmented path needs curl')
class TestRealTransfer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), _RangeHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_address[1]}/file.bin'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()          # shutdown stops accepting; this closes the fd

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def target(self, name):
        return os.path.join(self.dir, name)

    def fold(self, content=DATA, piece=PIECE):
        outer = hashlib.sha1()
        for offset in range(0, len(content), piece):
            outer.update(hashlib.sha1(content[offset:offset + piece]).digest())
        return outer.hexdigest().upper()

    def test_four_segments_splice_into_the_exact_file(self):
        path = self.target('four.bin')
        got = stream.download_segments(lambda: self.url, path, len(DATA), 4,
                                       log=lambda *a: None)
        self.assertEqual(got, len(DATA))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), DATA)

    def test_a_single_stream_resumes_from_the_bytes_it_already_has(self):
        path = self.target('resumed.bin')
        half = len(DATA) // 2
        with open(path, 'wb') as handle:
            handle.write(DATA[:half])
        written = stream.download_stream(self.url, path, len(DATA), resume_from=half)
        self.assertEqual(written, len(DATA))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), DATA)

    def test_a_segment_written_past_its_range_is_refetched_not_truncated(self):
        path = self.target('poisoned.bin')
        plan = stream.plan_segments(len(DATA), 4, path + '.segs4')
        for item in plan:
            with open(item['path'], 'wb') as handle:
                handle.write(DATA[item['start']:item['end'] + 1] + b'X' * 4096)
        self.assertEqual(sorted(stream.wipe_overshot(plan)), [0, 1, 2, 3])
        self.assertFalse(any(os.path.exists(item['path']) for item in plan))
        stream.download_segments(lambda: self.url, path, len(DATA), 4, log=lambda *a: None)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), DATA)

    def test_the_content_hash_rule_reproduces_the_server_fold(self):
        path = self.target('hash.bin')
        stream.download_segments(lambda: self.url, path, len(DATA), 4, log=lambda *a: None)
        digest = self.fold()
        self.assertEqual(stream.content_hash(path, PIECE), digest)
        self.assertEqual(stream.verify_content(path, digest), PIECE)

    def test_a_right_length_shifted_middle_file_fails_the_hash(self):
        # the failure mode the byte count cannot see: a duplicated stretch pushes the
        # rest of the segment off, and the segment is then short by exactly the overlap
        damaged = bytearray(DATA)
        damaged[PIECE + 1000:PIECE + 400000] = DATA[PIECE:PIECE + 399000]
        self.assertEqual(len(damaged), len(DATA))
        path = self.target('damaged.bin')
        with open(path, 'wb') as handle:
            handle.write(bytes(damaged))
        self.assertIsNone(stream.verify_content(path, self.fold()))
        self.assertEqual(os.path.getsize(path), len(DATA))


if __name__ == '__main__':
    unittest.main()
