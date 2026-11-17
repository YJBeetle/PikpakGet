"""Regression scenarios from logs, including a real truncated HTTP transfer."""
import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from pikpakget.cli import build_parser
from pikpakget.errors import PikPakError, RestoreOutcomeUnknown
from pikpakget.pipeline import Pipeline, job_key


PAYLOAD = b'network-recovery-' * 1024


class TestTaskNetworkRecovery(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        args = build_parser().parse_args(['--dest', root.name, '--repeat', '3', '--gap', '0'])
        args.state_dir = os.path.join(root.name, 'Series', '.pikpakget')
        args.series_root = os.path.join(root.name, 'Series')
        self.messages = []
        self.pipeline = Pipeline(args, lambda message, level='info': self.messages.append(message))
        self.pipeline.space = mock.Mock(return_value={
            'limit': 6 << 30, 'usage': 0, 'in_trash': 0, 'free': 6 << 30})
        self.pipeline.wait_gap = mock.Mock(return_value=False)
        self.job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111',
                    'share_id': 'EXAMPLEID1111111111', 'folder': 'Series',
                    'pass_code': '', 'order': 1}

    def test_zero_progress_network_failure_uses_all_three_passes(self):
        self.pipeline.inventory = mock.Mock(side_effect=PikPakError('TLS EOF', network=True))
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.inventory.call_count, 3)
        self.assertEqual([c.args[0] for c in self.pipeline.wait_gap.call_args_list], [5, 15])
        self.assertEqual(self.pipeline.state.link(job_key(self.job))['attempts'], 0)
        self.assertEqual(self.pipeline.state.link(job_key(self.job))['status'], 'retry')
        self.assertFalse(any('链接不可用' in m or '零进展' in m for m in self.messages))

    def test_permanent_share_refusal_keeps_zero_progress_guard(self):
        self.pipeline.inventory = mock.Mock(side_effect=PikPakError('revoked', status=404))
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.inventory.call_count, 1)
        self.pipeline.wait_gap.assert_not_called()

    def test_initial_quota_outage_retries_without_relogin_or_account_rotation(self):
        self.pipeline.space.side_effect = PikPakError('Connection refused', network=True)
        self.pipeline.inventory = mock.Mock()
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.space.call_count, 3)
        self.pipeline.inventory.assert_not_called()

    def test_setup_outage_does_not_skip_or_mark_account_capped(self):
        self.pipeline.account_id = 'account'
        self.pipeline.client.prepare_workspace = mock.Mock(side_effect=PikPakError('EOF', network=True))
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.client.prepare_workspace.call_count, 3)
        self.assertFalse(self.pipeline.traffic_capped)

    def configure_transfer(self):
        node = {'id': 'source', 'name': 'file.bin', 'size': len(PAYLOAD),
                'hash': None, 'path': 'file.bin', 'parent_id': '', 'is_folder': False}
        report = {'title': 'Series', 'token': 'share-token', 'files': [node],
                  'total': len(PAYLOAD), 'truncated': False, 'folder_nodes': []}
        self.pipeline.inventory = mock.Mock(return_value=report)
        self.pipeline.workspace_id = 'pack'
        self.pipeline.account_id = 'account'
        self.pipeline.client.list_folder = lambda folder: []
        self.pipeline.reuse_or_restore = mock.Mock(return_value='copy')
        self.pipeline.wait_ready = lambda *args: None
        self.pipeline.client.file = lambda file_id: {'kind': 'drive#file', 'size': len(PAYLOAD),
                                                     'hash': None, 'trashed': False}
        self.pipeline.client.download_url = lambda file_id: (self.url, {})
        self.pipeline.created.add('copy')
        self.pipeline.client.cleanup = mock.Mock()
        self.pipeline.wait_space_freed = lambda *args: None
        return node

    def start_server(self, truncate_first=True):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
                requests.append(start)
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(PAYLOAD) - start))
                self.end_headers()
                body = PAYLOAD[start:]
                if len(requests) == 1 and truncate_first:
                    body = body[:1024]
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(close)
        self.url = f'http://127.0.0.1:{server.server_address[1]}/file'
        return requests

    def test_real_short_http_transfer_resumes_after_zero_completion_pass(self):
        requests = self.start_server()
        self.configure_transfer()
        # Restore records the ID before transfer, as the real restore_one does.
        def restore(job, node, record, token):
            record.update({'restored_id': 'copy', 'account_id': 'account'})
            return 'copy'
        self.pipeline.reuse_or_restore.side_effect = restore
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(requests, [0, 1024])
        self.assertEqual(self.pipeline.state.file('source')['attempts'], 1)
        self.assertEqual(self.pipeline.state.file('source')['state'], 'done')
        self.pipeline.client.cleanup.assert_called_once_with(['copy'])
        with open(os.path.join(self.pipeline.args.dest, 'Series', 'file.bin'), 'rb') as handle:
            self.assertEqual(handle.read(), PAYLOAD)

    def test_cleanup_outage_retries_cleanup_without_redownloading(self):
        requests = self.start_server(truncate_first=False)
        self.configure_transfer()
        def restore(job, node, record, token):
            record.update({'restored_id': 'copy', 'account_id': 'account'})
            return 'copy'
        self.pipeline.reuse_or_restore.side_effect = restore
        self.pipeline.client.cleanup.side_effect = [PikPakError('TLS EOF', network=True), None]
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(requests, [0])
        self.assertEqual(self.pipeline.client.cleanup.call_count, 2)
        self.assertEqual(self.pipeline.state.file('source')['state'], 'done')

    def test_cloud_copy_probe_outage_preserves_copy_and_retries_before_transfer(self):
        requests = self.start_server(truncate_first=False)
        self.configure_transfer()
        record = self.pipeline.state.file('source', job_key(self.job))
        record.update({'restored_id': 'copy', 'account_id': 'account'})
        self.pipeline.client.file = mock.Mock(side_effect=[
            PikPakError('TLS EOF', network=True),
            {'kind': 'drive#file', 'size': len(PAYLOAD), 'trashed': False}])
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(requests, [0])
        self.pipeline.wait_gap.assert_called_once_with(5)
        self.assertEqual(self.pipeline.reuse_or_restore.call_count, 1)

    def test_space_reclamation_outage_is_not_recorded_as_file_too_large(self):
        self.start_server(truncate_first=False)
        self.configure_transfer()
        self.pipeline.space.return_value['free'] = 0
        failures = []

        def reclaim(needed):
            if needed and not failures:
                failures.append(needed)
                raise PikPakError('TLS EOF', network=True)
            return bool(needed)

        self.pipeline.reclaim_workspace = reclaim
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(self.pipeline.state.file('source')['state'], 'done')
        self.assertEqual(self.pipeline.state.file('source')['attempts'], 1)

    def test_uncertain_restore_is_not_repeated_or_cleaned_up(self):
        self.url = 'unused'
        self.configure_transfer()
        self.pipeline.reuse_or_restore.side_effect = RestoreOutcomeUnknown('reply lost', network=True)
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.reuse_or_restore.call_count, 1)
        self.pipeline.client.cleanup.assert_not_called()
        self.pipeline.wait_gap.assert_not_called()

    def test_cleanup_outage_does_not_refund_a_completed_transfer_slot(self):
        requests = self.start_server(truncate_first=False)
        node = self.configure_transfer()
        second = dict(node, id='second', name='second.bin', path='second.bin')
        self.pipeline.inventory.return_value['files'].append(second)
        self.pipeline.inventory.return_value['total'] += len(PAYLOAD)
        self.pipeline.files_left = 1
        self.pipeline.args.max_files = 1

        def restore(job, node, record, token):
            record.update({'restored_id': 'copy', 'account_id': 'account'})
            return 'copy'
        self.pipeline.reuse_or_restore.side_effect = restore
        self.pipeline.client.cleanup.side_effect = [PikPakError('TLS EOF', network=True), None]
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 1)  # limited run is incomplete
        self.assertEqual(requests, [0])
        self.assertEqual(self.pipeline.files_done, 1)
        self.assertEqual(self.pipeline.state.file('source')['state'], 'done')
        self.assertFalse(os.path.exists(os.path.join(self.pipeline.args.dest, 'Series', 'second.bin')))
