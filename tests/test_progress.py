"""Progress output must coexist with logs and retain resumed byte accounting."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from pikpakget.pipeline import Log
from pikpakget.progress import TransferProgress
from pikpakget.stream import download_segments, plan_segments


class TestProgress(unittest.TestCase):
    def test_segments_report_existing_bytes_without_redownloading(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'file.part')
            plan = plan_segments(8, 2, target + '.segs2')
            for item in plan:
                with open(item['path'], 'wb') as handle:
                    handle.write(b'x' * item['want'])
            updates = []
            self.assertEqual(download_segments(
                lambda: self.fail('completed segments need no request'), target, 8, 2,
                on_progress=lambda written, elapsed: updates.append(written)), 8)
            self.assertEqual(updates[0], 8)

    def test_redirected_output_is_sparse_plain_text_and_reaches_log(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(output):
            path = os.path.join(root, 'run.log')
            log = Log(path)
            with mock.patch('pikpakget.progress.time.monotonic', return_value=100):
                progress = TransferProgress(log, 2000, 1000)
                progress.render(True)
            progress.update(1500)
            with mock.patch('pikpakget.progress.time.monotonic', return_value=101):
                progress.render()
            self.assertEqual(output.getvalue().count('下载 ['), 1)
            with mock.patch('pikpakget.progress.time.monotonic', return_value=130):
                progress.render()
            log.close()
            self.assertEqual(output.getvalue().count('下载 ['), 2)
            self.assertNotIn('\r', output.getvalue())
            self.assertIn('75.0%', output.getvalue())
            with open(path) as handle:
                self.assertIn('75.0%', handle.read())

    def test_normal_log_clears_terminal_bar_and_file_has_no_escape_codes(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(output):
            path = os.path.join(root, 'run.log')
            log = Log(path)
            log.is_terminal = True
            log.transfer('下载 50%')
            log('网络警告', 'warn')
            log.close()
            self.assertIn('\r\033[2K', output.getvalue())
            self.assertIn('网络警告', output.getvalue())
            with open(path) as handle:
                saved = handle.read()
            self.assertNotIn('\033', saved)
            self.assertIn('网络警告', saved)

    def test_quiet_suppresses_progress(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = Log(quiet=True)
            with TransferProgress(log, 100) as progress:
                progress.update(50)
        self.assertEqual(output.getvalue(), '')

    def test_stalled_transfer_reports_zero_speed(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = Log()
            log.is_terminal = True
            with mock.patch('pikpakget.progress.time.monotonic', return_value=0):
                progress = TransferProgress(log, 200, 100)
            for stamp in (1, 3, 7):
                with mock.patch('pikpakget.progress.time.monotonic', return_value=stamp):
                    progress.render(True)
        self.assertIn('0.0B/s', output.getvalue())
        self.assertIn('剩余 未知', output.getvalue())

    def test_verbose_gates_debug_lines(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = Log()
            log('阶段细节', 'debug')
            self.assertEqual(output.getvalue(), '')
            log.verbose = True
            log('阶段细节', 'debug')
        self.assertIn('阶段细节', output.getvalue())

    def test_terminal_mode_logs_sparse_plain_progress(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(output):
            path = os.path.join(root, 'run.log')
            log = Log(path)
            log.is_terminal = True
            for stamp, message in ((1000, '下载 10%'), (1100, '下载 20%'),
                                   (1400, '下载 30%')):
                with mock.patch('pikpakget.pipeline.time.monotonic', return_value=stamp):
                    log.transfer(message)
            log.close()
            with open(path) as handle:
                saved = handle.read()
            self.assertEqual(saved.count('下载 '), 2)
            self.assertNotIn('\033', saved)
            self.assertEqual(output.getvalue().count('\r\033[2K'), 3)

    def test_eta_uses_the_minute_average_not_the_recent_dip(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = Log()
            log.is_terminal = True
            with mock.patch('pikpakget.progress.time.monotonic', return_value=0):
                progress = TransferProgress(log, 2200, 0)
                progress.render(True)
            for stamp, written in ((25, 550), (50, 1100), (55, 1100)):
                progress.update(written)
                with mock.patch('pikpakget.progress.time.monotonic', return_value=stamp):
                    progress.render(True)
        last = output.getvalue().splitlines()[-1]
        self.assertIn('0.0B/s', last)          # the dip is shown honestly
        self.assertIn('剩余 0分55秒', last)     # but ETA keeps the longer-window average


if __name__ == '__main__':
    unittest.main()
