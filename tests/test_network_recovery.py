"""Regression scenarios from logs, including a real truncated HTTP transfer."""
import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from pikpakget import cli
from pikpakget.cli import build_parser
from pikpakget.errors import PikPakError, RestoreOutcomeUnknown, TrafficCapped
from pikpakget.pipeline import Pipeline, job_key


PAYLOAD = b'network-recovery-' * 1024


class TestTaskNetworkRecovery(unittest.TestCase):
    def test_repeat_default_is_one_and_explicit_zero_is_unlimited(self):
        self.assertEqual(build_parser().parse_args([]).repeat, 1)
        self.assertEqual(build_parser().parse_args(['--repeat', '0']).repeat, 0)

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

    def test_three_consecutive_failures_stop_within_one_catalogue_pass(self):
        self.pipeline.inventory = mock.Mock(side_effect=PikPakError('TLS EOF', network=True))
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.inventory.call_count, 3)
        self.assertEqual([c.args[0] for c in self.pipeline.wait_gap.call_args_list], [5, 15])
        self.assertEqual(self.pipeline.state.link(job_key(self.job))['attempts'], 0)
        self.assertEqual(self.pipeline.state.link(job_key(self.job))['status'], 'retry')
        self.assertFalse(any('链接不可用' in m or '零进展' in m for m in self.messages))
        self.assertEqual(sum(m.startswith('— 第') for m in self.messages), 1)

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

    def start_server(self, truncate_first=True, truncate_requests=None):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
                requests.append(start)
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(PAYLOAD) - start))
                self.end_headers()
                body = PAYLOAD[start:]
                if ((truncate_requests is not None and len(requests) in truncate_requests)
                        or (truncate_requests is None and len(requests) == 1 and truncate_first)):
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

    def test_successful_files_reset_counter_despite_four_separate_disconnects(self):
        self.pipeline.args.repeat = 1
        requests = self.start_server(truncate_requests={1, 3, 5, 7})
        node = self.configure_transfer()
        report = self.pipeline.inventory.return_value
        report['files'] = [dict(node, id=f'source{i}', name=f'file{i}.bin', path=f'file{i}.bin')
                           for i in range(4)]
        report['total'] = len(PAYLOAD) * 4

        def restore(job, node, record, token):
            copied = 'copy-' + node['id']
            record.update({'restored_id': copied, 'account_id': 'account'})
            self.pipeline.created.add(copied)
            return copied

        self.pipeline.reuse_or_restore.side_effect = restore
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(requests, [0, 1024] * 4)
        self.assertEqual(self.pipeline.files_done, 4)
        self.assertEqual(self.pipeline.consecutive_failures, 0)
        self.assertEqual(self.pipeline.client.cleanup.call_count, 4)
        self.assertEqual([c.args[0] for c in self.pipeline.wait_gap.call_args_list], [5] * 4)
        self.assertEqual(sum(m.startswith('— 第') for m in self.messages), 1)

    def test_partial_progress_and_existing_done_files_do_not_reset_failures(self):
        self.pipeline.args.repeat = 0
        requests = self.start_server(truncate_requests={1, 2, 3})
        node = self.configure_transfer()
        complete = dict(node, id='old', name='old.bin', path='old.bin')
        self.pipeline.inventory.return_value['files'].insert(0, complete)
        root = os.path.join(self.pipeline.args.dest, 'Series')
        os.makedirs(root)
        path = os.path.join(root, 'old.bin')
        with open(path, 'wb') as handle:
            handle.write(PAYLOAD)
        self.pipeline.state.file('old', job_key(self.job)).update(
            {'state': 'done', 'local': path, 'home': path, 'size': len(PAYLOAD)})
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(requests, [0, 1024, 2048])
        self.assertEqual(self.pipeline.consecutive_failures, 3)
        self.assertTrue(self.pipeline.transient_limit_reached)
        self.assertEqual(os.path.getsize(os.path.join(root, 'file.bin.part')), 3072)
        self.pipeline.client.cleanup.assert_not_called()

    def test_finishing_pending_cleanup_resets_failures_before_next_file(self):
        self.pipeline.args.repeat = 1
        requests = self.start_server(truncate_requests={2})
        node = self.configure_transfer()
        self.pipeline.inventory.return_value['files'].append(
            dict(node, id='second', name='second.bin', path='second.bin'))

        def restore(job, node, record, token):
            copied = 'copy-' + node['id']
            record.update({'restored_id': copied, 'account_id': 'account'})
            self.pipeline.created.add(copied)
            return copied

        self.pipeline.reuse_or_restore.side_effect = restore
        self.pipeline.client.cleanup.side_effect = [
            PikPakError('TLS EOF', network=True), PikPakError('TLS EOF', network=True), None, None]
        with mock.patch('pikpakget.pipeline.LOCAL_HEADROOM', 0):
            self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(requests, [0, 0, 1024])
        self.assertEqual(self.pipeline.files_done, 2)
        self.assertEqual(self.pipeline.consecutive_failures, 0)

    def test_zero_repeat_allows_more_than_three_ordinary_passes(self):
        self.pipeline.args.repeat = 0
        calls = []

        def run_link(job):
            calls.append(job)
            self.pipeline.files_done += 1
            self.pipeline.state.link(job_key(job))['status'] = 'done' if len(calls) == 5 else 'partial'
            return 'ok' if len(calls) == 5 else 'incomplete'

        self.pipeline.run_link = run_link
        self.assertEqual(self.pipeline.run([self.job]), 0)
        self.assertEqual(len(calls), 5)
        self.pipeline.wait_gap.assert_not_called()

    def test_one_repeat_limits_ordinary_passes_without_network_failures(self):
        self.pipeline.args.repeat = 1
        calls = []

        def run_link(job):
            calls.append(job)
            self.pipeline.files_done += 1
            self.pipeline.state.link(job_key(job))['status'] = 'partial'
            return 'incomplete'

        self.pipeline.run_link = run_link
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(len(calls), 1)

    def test_recovery_wait_can_be_interrupted_without_retrying(self):
        self.pipeline.args.repeat = 0
        self.pipeline.inventory = mock.Mock(side_effect=PikPakError('TLS EOF', network=True))
        self.pipeline.wait_gap.return_value = True
        self.assertEqual(self.pipeline.run([self.job]), 1)
        self.assertEqual(self.pipeline.inventory.call_count, 1)
        self.assertEqual(self.pipeline.consecutive_failures, 1)

    def test_account_rotation_does_not_reset_the_failure_streak(self):
        self.pipeline.args.recovery_state = self.pipeline.recovery
        self.pipeline.inventory = mock.Mock(side_effect=[
            PikPakError('TLS EOF', network=True), PikPakError('TLS EOF', network=True),
            TrafficCapped('daily cap', status=400)])
        self.assertEqual(self.pipeline.run([self.job]), 4)
        self.assertEqual(self.pipeline.consecutive_failures, 2)

        next_account = Pipeline(self.pipeline.args, lambda *args: None,
                                account_id='second', workspace_id='pack')
        next_account.space = self.pipeline.space
        next_account.reclaim_workspace = lambda needed: False
        next_account.wait_gap = mock.Mock(return_value=False)
        next_account.inventory = mock.Mock(side_effect=PikPakError('TLS EOF', network=True))
        self.assertEqual(next_account.run([self.job]), 1)
        self.assertTrue(next_account.transient_limit_reached)
        self.assertEqual(next_account.inventory.call_count, 1)
        self.assertFalse(next_account.traffic_capped)
        next_account.wait_gap.assert_not_called()

    def test_series_in_one_command_share_the_same_recovery_state(self):
        root = self.pipeline.args.dest
        path = os.path.join(root, 'links.txt')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(self.job['url'] + '\tFirst\n' + self.job['url'] + '\tSecond\n')
        args = build_parser().parse_args([path, '--dest', root, '--log', '-'])
        args.account_dir = os.path.join(root, 'accounts-home')
        states = []

        def run_group(selected, log, registry, jobs):
            states.append(selected.recovery_state)
            if len(states) == 1:
                selected.recovery_state.failed()
            else:
                self.assertEqual(selected.recovery_state.failures, 1)
            return 0, 0

        with mock.patch.object(cli, '_run_group', side_effect=run_group), \
                mock.patch.object(cli, '_install_stop_handler'), mock.patch.object(cli, 'STOP', False):
            self.assertEqual(cli._commands(args, lambda *args: None), 0)
        self.assertEqual(len(states), 2)
        self.assertIs(states[0], states[1])

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
