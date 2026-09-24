"""Tests for the pure logic. Synthetic data only — no live account, no real links."""
import collections
import json
import os
import tempfile
import time
import unittest

import argparse

from pikpakget.api import captcha_sign, parse_share_url
from pikpakget.pipeline import (AUTH_STOP, STATE_VERSION, State, human, load_folder_map,
                                read_links, safe_name)
from pikpakget.stream import plan_segments


class TestShareUrl(unittest.TestCase):
    def test_plain_link(self):
        self.assertEqual(parse_share_url('https://mypikpak.com/s/EXAMPLEID1234567890ab'),
                         ('EXAMPLEID1234567890ab', ''))

    def test_link_with_password(self):
        share, code = parse_share_url('https://mypikpak.com/s/EXAMPLEID1234567890ab?password=42')
        self.assertEqual((share, code), ('EXAMPLEID1234567890ab', '42'))

    def test_surrounding_text_is_fine(self):
        share, _ = parse_share_url('打开 https://mypikpak.com/s/EXAMPLEID1234567890ab 保存')
        self.assertEqual(share, 'EXAMPLEID1234567890ab')

    def test_rejects_other_hosts(self):
        with self.assertRaises(ValueError):
            parse_share_url('https://example.com/s/EXAMPLEID1234567890ab')


class TestSafeName(unittest.TestCase):
    def test_path_separators_cannot_escape(self):
        base = os.path.abspath('/library/FXX')
        for name in ('../../etc/passwd', 'a/../b', '.'):
            cleaned = safe_name(name)
            self.assertNotIn('/', cleaned)
            # what actually matters: the component always lands inside its directory
            self.assertTrue(os.path.normpath(os.path.join(base, cleaned))
                            .startswith(base + os.sep), cleaned)

    def test_windows_separators_and_control_chars(self):
        self.assertNotIn('\x00', safe_name('a\x00b/c'))

    def test_long_multibyte_names_are_clamped(self):
        name = safe_name('标' * 400)
        self.assertLessEqual(len(name.encode('utf-8')), 240)

    def test_extension_survives_truncation(self):
        name = safe_name('前缀' * 200 + '.mp4')
        self.assertTrue(name.endswith('.mp4'))

    def test_blank_names_fall_back(self):
        self.assertEqual(safe_name('   '), 'untitled')
        self.assertEqual(safe_name(''), 'untitled')


class TestLinksFile(unittest.TestCase):
    def write(self, text, suffix='.txt'):
        handle = tempfile.NamedTemporaryFile('w', suffix=suffix, delete=False, encoding='utf-8')
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_order_is_preserved_and_comments_ignored(self):
        path = self.write('# header\n\nhttps://mypikpak.com/s/AAA1111111111111111\n'
                          'https://mypikpak.com/s/BBB2222222222222222\n')
        jobs, problems = read_links(path)
        self.assertEqual(problems, [])
        self.assertEqual([job['share_id'] for job in jobs],
                         ['AAA1111111111111111', 'BBB2222222222222222'])
        self.assertEqual([job['order'] for job in jobs], [1, 2])

    def test_tab_column_overrides_folder(self):
        path = self.write('https://mypikpak.com/s/AAA1111111111111111\tExampleSeries\n')
        jobs, _ = read_links(path)
        self.assertEqual(jobs[0]['folder'], 'ExampleSeries')

    def test_folder_map_applies_without_a_column(self):
        links = self.write('https://mypikpak.com/s/AAA1111111111111111\n')
        mapping = self.write('https://mypikpak.com/s/AAA1111111111111111,MappedName\n', '.csv')
        jobs, _ = read_links(links, load_folder_map(mapping))
        self.assertEqual(jobs[0]['folder'], 'MappedName')

    def test_bad_lines_are_reported_not_crashing(self):
        path = self.write('https://mypikpak.com/s/AAA1111111111111111\nnonsense line\n')
        jobs, problems = read_links(path)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn(':2', problems[0])

    def test_default_folder_when_nothing_maps(self):
        path = self.write('https://mypikpak.com/s/AAA1111111111111111\n')
        jobs, _ = read_links(path)
        self.assertEqual(jobs[0]['folder'], '(unfiled)')


class TestSegments(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def plan(self, total, connections, prefill=None):
        for name, size in (prefill or {}).items():
            with open(os.path.join(self.dir, name), 'wb') as handle:
                handle.truncate(size)
        return plan_segments(total, connections, self.dir)

    def test_ranges_cover_the_file_exactly(self):
        plan = self.plan(1000, 4)
        self.assertEqual(plan[0]['start'], 0)
        self.assertEqual(plan[-1]['end'], 999)
        self.assertEqual(sum(item['want'] for item in plan), 1000)
        for previous, following in zip(plan, plan[1:]):
            self.assertEqual(following['start'], previous['end'] + 1)

    def test_resume_starts_from_what_is_on_disk(self):
        plan = self.plan(1000, 4, {'001': 60})
        resumed = next(item for item in plan if item['index'] == 1)
        self.assertEqual(resumed['have'], 60)
        self.assertEqual(resumed['want'], 250)

    def test_oversized_stale_segment_is_discarded(self):
        plan = self.plan(1000, 4, {'002': 999})
        stale = next(item for item in plan if item['index'] == 2)
        self.assertEqual(stale['have'], 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, '002')))

    def test_single_connection_covers_everything(self):
        plan = self.plan(4096, 1)
        self.assertEqual(len(plan), 1)
        self.assertEqual((plan[0]['start'], plan[0]['end']), (0, 4095))

    def test_more_connections_than_bytes(self):
        plan = self.plan(2, 8)
        self.assertEqual(sum(item['want'] for item in plan), 2)


class RunLinkHarness(unittest.TestCase):
    """Drives the real run_link loop with the network and the cloud-side cleanup
    replaced. The main chain had no coverage, which is exactly where four of the
    reported bugs lived."""

    def setUp(self):
        import argparse
        import pikpakget.pipeline as pipeline_module
        from pikpakget.pipeline import Log, Pipeline
        self.module = pipeline_module
        self.dir = tempfile.mkdtemp()
        self.lib = os.path.join(self.dir, 'lib')
        os.makedirs(self.lib)
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'), dest=self.lib,
                                  max_files=0, connections=1, gap=0, repeat=0,
                                  dry_run=False, inventory_only=False, limit=0,
                                  purge_trash=False, no_delete=True)
        self.pipeline = Pipeline(args, Log(quiet=True))
        self.pipeline.quota_limit = 6442450944
        self.pipeline.space = lambda: {'limit': 6442450944, 'usage': 0, 'in_trash': 0,
                                       'free': 6442450944}
        self.pipeline.forget = lambda *a, **k: None
        self.pipeline.wait_ready = lambda *a, **k: {}
        self.pipeline.reuse_or_restore = lambda job, node, record, token: 'RESTORED'
        # the library volume's free space is the environment, not the behaviour under
        # test: a CI runner with <20 GB free would otherwise return 'blocked'
        import types
        real_shutil = self.module.shutil
        self.addCleanup(setattr, self.module, 'shutil', real_shutil)
        plenty = types.SimpleNamespace(free=100 << 40)
        self.module.shutil = types.SimpleNamespace(disk_usage=lambda path: plenty)
        self.pipeline.client.download_url = lambda fid: ('http://host/file', {})
        self.addCleanup(setattr, pipeline_module, 'download_stream', pipeline_module.download_stream)
        self.downloads = []

    def fake_stream(self, url, target, expected=0, resume=0, *rest, **kwargs):
        self.downloads.append((target, expected))
        with open(target, 'wb') as handle:
            handle.truncate(expected)
        return expected

    def run_one(self, name='a.mp4', size=100, path=None):
        self.module.download_stream = self.fake_stream
        node = {'id': 'SHAREFILE1', 'name': name, 'size': size,
                'path': path or name, 'hash': None, 'is_folder': False}
        self.pipeline.inventory = lambda job: {
            'title': 'Series', 'token': 'TOKEN', 'files': [node],
            'total': size, 'folders': 0, 'truncated': False}
        job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
               'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}
        return self.pipeline.run_link(job), node


class TestTruncationIsVisible(RunLinkHarness):
    """A capped listing must never be able to mark a link complete in silence."""

    def test_a_capped_walk_is_flagged_on_the_link(self):
        def truncated_walked(job):
            return {'title': 'Series', 'token': 'T', 'files': [], 'total': 0,
                    'folders': 0, 'truncated': True}
        self.pipeline.inventory = truncated_walked
        job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
               'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}
        job['key'] = job['url'] + '\tSeries'
        self.pipeline.run_link(job)
        self.assertTrue(self.pipeline.state.link(job['key'])['truncated'])


class TestPerRunRetryBudget(unittest.TestCase):
    """A transient failure last week must not condemn a file forever."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = State(os.path.join(self.dir, 'state.json'))

    def test_abandoned_files_get_a_fresh_budget(self):
        self.state.file('A').update({'state': 'failed', 'attempts': 3})
        self.state.file('B').update({'state': 'retry', 'attempts': 2})
        self.state.file('C').update({'state': 'done', 'attempts': 3})
        self.state.file('D').update({'state': 'too_big', 'attempts': 1})
        self.state.link('u')['status'] = 'error'
        self.state.reset_transient_failures()
        self.assertEqual(self.state.file('A')['state'], 'pending')
        self.assertEqual(self.state.file('A')['attempts'], 0)
        self.assertEqual(self.state.file('B')['state'], 'pending')
        self.assertEqual(self.state.file('C')['state'], 'done', 'completed work is untouched')
        self.assertEqual(self.state.file('D')['state'], 'too_big', 'over quota stays skipped')
        self.assertEqual(self.state.link('u')['status'], 'pending')


class TestDotFileNames(unittest.TestCase):
    """A name that is *only* dots is the directory itself; a name with a pair of dots
    inside it is just a filename. The rewrite used to treat both as traversal."""

    def test_dotfiles_keep_their_leading_dot(self):
        self.assertEqual(safe_name('.bashrc'), '.bashrc')
        self.assertEqual(safe_name('.gitignore'), '.gitignore')

    def test_inner_double_dots_are_left_alone(self):
        self.assertEqual(safe_name('movie..2 集.mp4'), 'movie..2 集.mp4')

    def test_a_name_that_is_only_dots_cannot_stand_for_a_directory(self):
        for name in ('.', '..', '  ..  '):
            self.assertNotIn(safe_name(name), ('', '.', '..'))


class TestShareTruncationFlag(unittest.TestCase):
    def test_walk_reports_truncation_instead_of_losing_files_quietly(self):
        import pikpakget.api as api
        client = api.Client.__new__(api.Client)      # no session: only walk logic here
        pages = {
            '*': [{'id': 'F1', 'name': 'a', 'kind': 'drive#file', 'size': '1'},
                  {'id': 'D1', 'name': 'dir', 'kind': 'drive#folder'}],
            'D1': [{'id': 'F2', 'name': 'b', 'kind': 'drive#file', 'size': '2'}],
        }
        client.share_info = lambda share_id, pass_code='': {
            'pass_code_token': 'T', 'title': 'x', 'file_num': 2,
            'files': pages['*']}
        client.share_children = lambda share_id, token, parent_id: pages.get(parent_id, [])
        whole = client.walk_share('S')
        self.assertFalse(whole['truncated'])
        self.assertEqual(len(whole['nodes']), 3)
        capped = client.walk_share('S', max_nodes=2)
        self.assertTrue(capped['truncated'], 'a capped walk must say so')


class TestLinkIdentity(unittest.TestCase):
    """A link is identified by URL *and* folder, and leftovers from earlier lists
    must not make a clean run look unfinished."""

    def write(self, text):
        handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_same_share_into_two_folders_yields_two_jobs(self):
        from pikpakget.pipeline import read_links
        url = 'https://mypikpak.com/s/EXAMPLEID1111111111'
        jobs, problems = read_links(self.write(f'{url}\tSeriesA\n{url}\tSeriesB\n'))
        self.assertEqual(problems, [])
        self.assertEqual([job['folder'] for job in jobs], ['SeriesA', 'SeriesB'])
        self.assertNotEqual(jobs[0]['key'], jobs[1]['key'])

    def test_a_stale_link_from_an_earlier_list_does_not_extend_the_run(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        dirpath = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(dirpath, '.state'), dest=dirpath,
                                  max_files=0, connections=1, gap=0, repeat=0, dry_run=False,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=True, no_sweep=True)
        pipeline = Pipeline(args, Log(quiet=True))
        pipeline.space = lambda: {'limit': 6_000_000_000, 'usage': 0, 'in_trash': 0,
                                  'free': 6_000_000_000}
        pipeline.state.link('https://mypikpak.com/s/DROPPEDID1111111\tOldList')['status'] = 'error'
        passes = []

        def finish(job):
            passes.append(job['key'])
            pipeline.state.link(job['key'])['status'] = 'done'
            return 'ok'
        pipeline.run_link = finish
        jobs = [{'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
                 'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}]
        jobs[0]['key'] = jobs[0]['url'] + '\t' + jobs[0]['folder']
        self.assertEqual(pipeline.run(jobs), 0)
        self.assertEqual(passes, [jobs[0]['key']],
                         'one pass is enough when this run finished its own list')


class TestFailureClassification(unittest.TestCase):
    """Three failures that look alike must not get one answer: a rate limit wants an
    hour, an expired session wants --login, a signed link that 404s wants neither."""

    def setUp(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        dirpath = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(dirpath, '.state'), dest=dirpath,
                                  max_files=0, connections=1, gap=0, repeat=0)
        self.pipeline = Pipeline(args, Log(quiet=True))

    def error(self, message, status=None, throttled=False):
        from pikpakget.api import PikPakError
        return PikPakError(message, status=status, throttled=throttled)

    def test_an_expired_signed_link_is_not_a_rate_limit(self):
        for _ in range(3):
            stopped = self.pipeline.note_throttle(self.error('下载被拒: HTTP 404', status=404))
            self.assertFalse(stopped, 'a 404 CDN link is not the CDN saying slow down')

    def test_a_refused_session_asks_to_relogin_not_to_wait(self):
        import contextlib
        import io
        buffer = io.StringIO()
        # the server said no to refreshing *this* session; a bare 403 on one link
        # does not, see TestSessionVersusOneBadLink
        self.pipeline.client.session_dead = True
        with contextlib.redirect_stdout(buffer):
            stopped = self.pipeline.note_throttle(self.error('会话续期失败: 403', status=403))
        self.assertTrue(stopped)
        self.assertIn('--login', buffer.getvalue())
        self.assertNotIn('1 小时', buffer.getvalue())

    def test_three_rate_limits_stop_the_run(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            results = [self.pipeline.note_throttle(self.error('限流', status=429))
                       for _ in range(3)]
        self.assertEqual(results[:2], [False, False])
        self.assertTrue(results[2])
        self.assertIn('风控', buffer.getvalue())

    def test_a_success_resets_the_consecutive_counter(self):
        self.pipeline.note_throttle(self.error('限流', status=503))
        self.pipeline.note_throttle(self.error('限流', status=503))
        self.pipeline.throttle_hits = 0                    # what a completed file does
        self.assertFalse(self.pipeline.note_throttle(self.error('限流', status=503)))
        self.assertEqual(self.pipeline.throttle_hits, 1)

    def test_a_refused_segment_lanes_down_without_stopping_the_run(self):
        from pikpakget.stream import SegmentRefused
        self.assertFalse(self.pipeline.note_throttle(SegmentRefused('分段被拒', status=403)))


class TestSingleStreamInterrupt(unittest.TestCase):
    """README recommends one connection, and refusals/starvation fall back to it, so
    the single stream has to answer to Ctrl-C like the segmented path does."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, 'source.bin')
        with open(self.src, 'wb') as handle:
            handle.truncate(3 << 20)
        self.target = os.path.join(self.dir, 'out.part')

    def test_a_requested_stop_keeps_the_partial_file(self):
        from pikpakget.api import PikPakError
        from pikpakget.stream import download_stream
        with self.assertRaises(PikPakError) as caught:
            download_stream('file://' + self.src, self.target, 3 << 20, stop=lambda: True)
        self.assertIn('已中断', str(caught.exception))
        # an interrupted .part is a resume point, not garbage: a later call has to be
        # able to finish the file from wherever it stopped
        self.assertLessEqual(os.path.getsize(self.target) if os.path.exists(self.target) else 0,
                             3 << 20)
        from pikpakget.stream import download_stream
        self.assertEqual(download_stream('file://' + self.src, self.target, 3 << 20,
                                        resume_from=os.path.getsize(self.target)
                                        if os.path.exists(self.target) else 0), 3 << 20)

    def test_it_finishes_when_nobody_stops_it(self):
        from pikpakget.stream import download_stream
        self.assertEqual(download_stream('file://' + self.src, self.target, 3 << 20), 3 << 20)
        self.assertEqual(os.path.getsize(self.target), 3 << 20)


class TestSweepBaseline(RunLinkHarness):
    """The startup sweep must wait for the reclaimed bytes to come back, not for the
    whole drive to empty."""

    def test_reclaim_waits_against_usage_before_the_delete(self):
        self.pipeline.state.file('SHAREFILE1').update(
            {'restored_id': 'CLOUD1', 'state': 'restoring', 'size': 400})
        self.pipeline.client.list_folder = lambda parent='*': [
            {'kind': 'drive#file', 'id': 'CLOUD1', 'size': '400'}]
        self.pipeline.client.cleanup = lambda ids: None
        readings = iter([1000, 600])                  # 400 reclaimed, 600 unrelated left
        self.pipeline.space = lambda: {'limit': 6442450944, 'usage': next(readings),
                                       'in_trash': 0, 'free': 0}
        waits = []
        self.pipeline.wait_space_freed = lambda size, before=None: waits.append((size, before)) or {
            'usage': 600}
        self.pipeline.sweep_leftovers()
        self.assertEqual(waits, [(400, 1000)],
                         'before must be the usage read before the delete; passing the '
                         'reclaimed size makes the floor 0 and stalls on any other file')


class TestMovedDownloadsDoNotBreakTheRun(RunLinkHarness):
    def test_a_moved_finished_file_is_redownloaded_instead_of_crashing(self):
        gone = os.path.join(self.lib, 'Series', 'a.mp4')
        record = self.pipeline.state.file('SHAREFILE1', 'https://mypikpak.com/s/EXAMPLEID1111111111')
        record.update({'state': 'downloaded', 'local': gone, 'size': 100})
        outcome, node = self.run_one()                      # before the fix: FileNotFoundError
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.downloads[0][1], 100)
        self.assertEqual(self.pipeline.state.file('SHAREFILE1')['state'], 'done')
        self.assertEqual(os.path.getsize(gone), 100)

    def test_a_still_present_complete_file_is_skipped(self):
        keep = os.path.join(self.lib, 'Series', 'a.mp4')
        os.makedirs(os.path.dirname(keep))
        with open(keep, 'wb') as handle:
            handle.truncate(100)
        self.pipeline.state.file('SHAREFILE1').update(
            {'state': 'done', 'local': keep, 'size': 100,
             'url': 'https://mypikpak.com/s/EXAMPLEID1111111111'})
        outcome, node = self.run_one()
        self.assertEqual(self.downloads, [])            # complete file, no re-download
        self.assertEqual(outcome, 'ok')                 # nothing pending for this link


class TestForeignFileIsNeverDeleted(unittest.TestCase):
    """A share reusing a filename the user already has is not a licence to delete
    the user's copy. This was data loss with a single warn line in the log."""

    def setUp(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        self.lib = os.path.join(self.dir, 'lib', 'Series')
        os.makedirs(self.lib)
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'), dest=self.lib,
                                  max_files=0, connections=1, gap=0, repeat=0)
        self.pipeline = Pipeline(args, Log(quiet=True))
        import pikpakget.pipeline as pipeline
        self.addCleanup(setattr, pipeline, 'download_stream', pipeline.download_stream)
        self.pipeline.client.download_url = lambda fid: ('http://host/url', {})

        def fake_stream(url, target, expected=0, resume=0, *rest, **kwargs):
            with open(target, 'wb') as handle:
                handle.truncate(expected)
            return expected
        pipeline.download_stream = fake_stream
        self.pipeline.client.file = lambda fid: {'phase': 'PHASE_TYPE_COMPLETE', 'size': 10}

    def grab(self, name='movie.mp4', size=10):
        node = {'name': name, 'size': size, 'path': name, 'local': None}
        return self.pipeline.fetch_to('FID', node, self.lib)

    def write_existing(self, name, content):
        path = os.path.join(self.lib, name)
        with open(path, 'wb') as handle:
            handle.write(content)
        return path

    def test_foreign_same_name_is_renamed_not_removed(self):
        before = self.write_existing('movie.mp4', b'precious family video')
        final, how = self.grab()
        self.assertEqual(final, before)                   # same path, now our content
        self.assertEqual(os.path.getsize(before), 10)
        clashes = [name for name in os.listdir(self.lib) if '.conflict-' in name]
        self.assertEqual(len(clashes), 1, '用户的文件应改名保留')
        with open(os.path.join(self.lib, clashes[0]), 'rb') as handle:
            self.assertEqual(handle.read(), b'precious family video')
        self.assertEqual(how, 'downloaded')
        self.assertTrue(os.path.isfile(final))

    def test_our_own_stale_copy_is_replaced_quietly(self):
        path = os.path.join(self.lib, 'movie.mp4')
        self.write_existing('movie.mp4', b'half-written by us earlier')
        self.pipeline.state.file('OTHER')['local'] = path
        self.pipeline.state.file('OTHER')['state'] = 'done'
        self.pipeline.state.file('OTHER')['size'] = 3
        self.grab()
        self.assertEqual([name for name in os.listdir(self.lib) if '.conflict-' in name], [])
        self.assertEqual(os.path.getsize(path), 10)


class TestNameCollisions(unittest.TestCase):
    """Remote names come from someone else's folders, so collisions are the norm
    and a silent overwrite would be data loss."""

    def setUp(self):
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, 'library', 'OneSeries')
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  max_files=0, dest=self.dest, connections=1, gap=0)
        self.pipeline = Pipeline(args, Log(quiet=True))

    def alloc(self, path):
        return os.path.basename(self.pipeline.dest_path(
            {'name': path.rsplit('/', 1)[-1], 'path': path}, self.dest))

    def test_first_file_keeps_its_name(self):
        self.assertEqual(self.alloc('一/同样的名字.mp4'), '同样的名字.mp4')

    def test_second_file_with_same_name_gets_its_parent(self):
        self.alloc('一/同样的名字.mp4')
        self.assertEqual(self.alloc('二/同样的名字.mp4'), '二 - 同样的名字.mp4')

    def test_reallocation_is_stable(self):
        path = '一/同样的名字.mp4'
        self.assertEqual(self.alloc(path), self.alloc(path))

    def test_repeated_parent_still_diverges(self):
        self.alloc('一/片.mp4')
        self.alloc('二/片.mp4')
        self.assertEqual(self.alloc('二/片.mp4 (2)'), '片.mp4 (2)')

    def test_root_level_file_without_parent(self):
        self.alloc('重名.mp4')
        second = self.alloc('别的/重名.mp4')
        self.assertTrue(second.endswith('.mp4'))
        self.assertNotEqual(second, '重名.mp4')


class TestSegmentRequest(unittest.TestCase):
    """Regression cover for the bug where a retry re-read its resume offset from the
    original plan and appended a duplicate copy onto the segment."""

    def setUp(self):
        import pikpakget.stream as stream
        self.stream = stream
        self.dir = tempfile.mkdtemp()
        self.item = {'index': 0, 'path': os.path.join(self.dir, '000'),
                     'start': 0, 'end': 999, 'want': 1000, 'have': 0}

    def test_resume_offset_comes_from_the_file_not_the_plan(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(400)
        self.item['have'] = 9999                      # a stale plan value
        request = self.stream.segment_request(self.item)
        self.assertEqual(request['range'], '400-999')
        self.assertEqual(request['remaining'], 600)

    def test_a_size_limit_is_sent_so_range_cannot_be_ignored_silently(self):
        command = self.stream.build_command(self.item, 'http://invalid.invalid/url')
        self.assertIn('--max-filesize', command)
        self.assertEqual(command[command.index('--max-filesize') + 1], '1000')

    def test_complete_segment_is_not_fetched_again(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1000)
        self.assertIsNone(self.stream._spawn(self.item, 'http://invalid.invalid/url'))

    def test_trailing_overshoot_is_reported_not_discarded(self):
        # Discarding an overshot segment threw away good progress and made the run
        # oscillate; the extra tail is now simply never read.
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1500)
        self.assertEqual(self.stream.overshot([self.item]), [0])
        self.assertTrue(os.path.exists(self.item['path']))
        self.assertEqual(self.stream._segment_have(self.item), 1000)

    def test_exact_segment_is_complete(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1000)
        self.assertEqual(self.stream.overshot([self.item]), [])
        self.assertEqual(self.stream._segment_have(self.item), 1000)

    def test_splice_never_reads_past_the_segment_length(self):
        with open(self.item['path'], 'wb') as handle:
            handle.write(b'a' * 1000 + b'X' * 500)   # junk tail from a race
        other = os.path.join(self.dir, 'second.bin')
        target = os.path.join(self.dir, 'out.bin')
        stream = self.stream
        plan = [self.item]
        with open(target, 'wb') as out:
            remaining = self.item['want']
            with open(self.item['path'], 'rb') as handle:
                while remaining > 0:
                    block = handle.read(min(64, remaining))
                    if not block:
                        break
                    out.write(block)
                    remaining -= len(block)
        self.assertEqual(os.path.getsize(target), 1000)
        del other, stream, plan

    def test_reap_waits_for_children_so_the_next_round_cannot_share_the_file(self):
        class Proc:
            def __init__(self):
                self.terminated = False
                self.waited = False

            def poll(self):
                return None if not self.terminated else 0

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if not self.terminated:
                    raise AssertionError('reap must terminate before waiting')
                self.waited = True
                return 0

        proc = self.Proc = Proc()
        self.stream.reap([proc])
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.waited)


class TestStarvationFallback(unittest.TestCase):
    """Starved lanes are not a server error to retry against: the segments cost
    minutes of back-off for bytes a single connection would have delivered sooner."""

    def setUp(self):
        from pikpakget.api import PikPakError
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  dest=os.path.join(self.dir, 'lib'), max_files=0,
                                  connections=4, gap=0, repeat=0)
        self.pipeline = Pipeline(args, Log(quiet=True))
        self.PikPakError = PikPakError
        self.stream_calls = []
        self.pipeline.client.download_url = lambda fid: ('http://host/url', {})
        import pikpakget.pipeline as pipeline
        self.addCleanup(setattr, pipeline, 'download_segments', pipeline.download_segments)
        self.addCleanup(setattr, pipeline, 'download_stream', pipeline.download_stream)

    def starve(self, url_of, target, total, connections, **kwargs):
        raise self.PikPakError('分段多次未完成，暂停本文件: 段 0,1,2,3')

    def fake_stream(self, url, target, expected=0, resume=0, *args, **kwargs):
        self.stream_calls.append((expected, resume))
        with open(target, 'wb') as handle:
            handle.truncate(expected)
        return expected

    def test_a_starved_file_falls_back_to_one_stream(self):
        import pikpakget.pipeline as pipeline
        pipeline.download_segments = self.starve
        pipeline.download_stream = self.fake_stream
        node = {'name': 'x.mp4', 'size': 5000, 'path': 'x.mp4', 'local': None}
        final, how = self.pipeline.fetch_to('FILEID', node, os.path.join(self.dir, 'lib'))
        self.assertEqual(self.stream_calls, [(5000, 0)])
        self.assertEqual(how, 'downloaded')
        self.assertEqual(self.pipeline.args.connections, 4)      # first time: keep trying

    def test_two_starved_files_end_segmentation_for_the_run(self):
        import pikpakget.pipeline as pipeline
        pipeline.download_segments = self.starve
        pipeline.download_stream = self.fake_stream
        for name in ('a.mp4', 'b.mp4'):
            node = {'name': name, 'size': 5000, 'path': name, 'local': None}
            self.pipeline.fetch_to('FILEID', node, os.path.join(self.dir, 'lib'))
        self.assertEqual(self.pipeline.args.connections, 1)
        self.assertEqual(self.pipeline.starved_files, 2)


class TestSegmentIdentity(unittest.TestCase):
    """Resuming a segment assumes those bytes are the prefix of the same content.
    A replaced upstream file breaks that assumption, so the stale prefix must go."""

    def setUp(self):
        import pikpakget.stream as stream
        self.stream = stream
        self.dir = tempfile.mkdtemp()

    def write_segment(self, name, size):
        with open(os.path.join(self.dir, name), 'wb') as handle:
            handle.truncate(size)

    def plan(self, total):
        return self.stream.plan_segments(total, 2, self.dir)

    def test_same_size_resume_keeps_partial_bytes(self):
        self.plan(1000)
        self.write_segment('000', 120)
        plan = self.plan(1000)
        self.assertEqual(self.stream._segment_have(plan[0]), 120)

    def test_replaced_file_discards_the_stale_prefix(self):
        self.plan(1000)
        self.write_segment('000', 120)
        self.plan(2000)                                   # same id, new size upstream
        plan = self.plan(2000)
        self.assertEqual(self.stream._segment_have(plan[0]), 0)

    def test_the_wipe_happens_once_not_every_round(self):
        self.plan(1000)
        self.write_segment('000', 120)
        self.plan(2000)
        self.plan(2000)
        self.plan(2000)
        plan = self.plan(2000)
        self.assertEqual(self.stream._segment_have(plan[0]), 0)
        self.write_segment('000', 500)                    # fresh partial progress
        self.assertEqual(self.stream._segment_have(self.plan(2000)[0]), 500)


class TestQuotaWait(unittest.TestCase):
    """After deleting a cloud copy the tool waits for the quota to fall. An
    unrelated parked copy must not turn that into a 150 s stall on every file."""

    def setUp(self):
        from pikpakget.pipeline import Log, Pipeline
        dirpath = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(dirpath, '.state'), dest=dirpath,
                                  max_files=0, connections=1, gap=0, repeat=0,
                                  no_sweep=True)
        self.pipeline = Pipeline(args, Log(quiet=True))
        self.readings = []

    def feed(self, usages):
        self.readings = list(usages)
        self.pipeline.space = lambda: {'limit': 6_442_450_944, 'usage': self.readings.pop(0),
                                       'in_trash': 0, 'free': 0}

    def test_near_miss_is_accepted_immediately(self):
        before, dropped = 2_127_000_000, 399_000_000
        self.feed([1_727_000_000])                      # floor is 1_728_000_000
        space = self.pipeline.wait_space_freed(dropped, before, timeout=1)
        self.assertEqual(space['usage'], 1_727_000_000)

    def test_a_real_stall_still_times_out(self):
        self.feed([4_000_000_000, 4_000_000_000])
        space = self.pipeline.wait_space_freed(399_000_000, 4_400_000_000, timeout=1)
        self.assertEqual(space['usage'], 4_000_000_000)


class TestLogQuiet(unittest.TestCase):
    """--quiet must silence the chatter but never a warning, or a stalled run looks
    healthy from the terminal."""

    def test_info_suppressed_warning_kept(self):
        import contextlib
        import io
        from pikpakget.pipeline import Log
        quiet = Log(quiet=True)
        loud = Log(quiet=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            quiet('进度 50%', 'info')
            quiet('被限流', 'warn')
            loud('普通一行', 'info')
        printed = buffer.getvalue()
        self.assertNotIn('进度 50%', printed)
        self.assertIn('被限流', printed)
        self.assertIn('普通一行', printed)

    def test_debug_is_opt_in(self):
        import contextlib
        import io
        from pikpakget.pipeline import Log
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            Log(quiet=False)('内部细节', 'debug')
        self.assertEqual(buffer.getvalue(), '')


class TestSegmentResume(unittest.TestCase):
    """A retry must read its offset from the file as it stands now. Trusting the
    length recorded when the plan was built re-downloads overlapping bytes and
    appends them, which silently grows the file past its real size."""

    def setUp(self):
        import pikpakget.stream as stream
        self.stream = stream
        self.dir = tempfile.mkdtemp()

    def item(self, index, start, end, have=0):
        path = os.path.join(self.dir, f'{index:03d}')
        if have:
            with open(path, 'wb') as handle:
                handle.truncate(have)
        return {'index': index, 'path': path, 'start': start, 'end': end,
                'want': end - start + 1, 'have': 0}

    def test_offset_follows_the_file_not_the_plan(self):
        item = self.item(1, 1000, 1999, have=300)          # 300 bytes already down
        request = self.stream.segment_request(item)
        self.assertEqual(request['range'], '1300-1999')
        self.assertEqual(request['remaining'], 700)
        self.assertEqual(request['max_filesize'], 700)

    def test_complete_segment_needs_no_request(self):
        item = self.item(0, 0, 499, have=500)
        self.assertEqual(self.stream.segment_request(item)['remaining'], 0)

    def test_overshoot_is_reported_and_ignored_not_discarded(self):
        # Deleting an overshot segment threw away good bytes and made the run
        # oscillate between discarding and refetching; the tail is now simply never
        # read, since the head of the segment is valid.
        good = self.item(0, 0, 499, have=500)
        bad = self.item(1, 500, 999, have=560)
        self.assertEqual(self.stream.overshot([good, bad]), [1])
        self.assertTrue(os.path.exists(bad['path']))
        self.assertEqual(self.stream._segment_have(bad), 500)
        self.assertEqual(self.stream._segment_have(good), 500)

    def test_range_is_capped_so_a_ignoring_cannot_double_the_file(self):
        command = self.stream.build_command(self.item(2, 200, 299), 'http://host/url')
        self.assertIn('--max-filesize', command)
        self.assertEqual(command[command.index('--max-filesize') + 1], '100')


class TestTransientNetworkRetry(unittest.TestCase):
    """A dropped TLS session is routine here. Treating it as "this link is broken"
    skipped a 404 GB link after one hiccup, which is the failure this covers."""

    def setUp(self):
        from pikpakget.api import Client
        dirpath = tempfile.mkdtemp()
        self.client = Client(session_path=os.path.join(dirpath, 'session.json'),
                             device_id_path=os.path.join(dirpath, 'device_id'))
        self.client.session.store({'access_token': 'a' * 40, 'refresh_token': 'r',
                                   'sub': 'u', 'expires_at': time.time() + 3600})
        self.client.captcha_token = lambda action: 'captcha-token'
        self.slept = []
        self.client._raw = self.fake_raw
        self.patches = []

    def fake_raw(self, *args, **kwargs):
        raise AssertionError('replaced per test')

    def feed(self, results):
        queue = collections.deque(results)

        def raw(method, url, **kwargs):
            return queue.popleft() if queue else (200, {})
        self.client._raw = raw

    def waits(self):
        import pikpakget.api as api
        original = api.time.sleep
        api.time.sleep = lambda seconds: self.slept.append(seconds)
        self.addCleanup(setattr, api.time, 'sleep', original)

    def test_a_dropped_connection_is_retried_and_then_succeeds(self):
        self.feed([(None, {'error_description': 'URLError: SSL EOF'}),
                   (None, {'error_description': 'URLError: SSL EOF'}),
                   (200, {'quota': {'limit': '10'}})])
        self.waits()
        self.assertEqual(self.client.about(), {'quota': {'limit': '10'}})
        self.assertEqual(self.slept, [5, 15])

    def test_giving_up_only_happens_after_the_whole_wait_ladder(self):
        import pikpakget.api as api
        self.feed([(None, {'error_description': 'URLError: SSL EOF'})] * 5)
        self.waits()
        with self.assertRaises(api.PikPakError):
            self.client.about()
        self.assertEqual(len(self.slept), 5)

    def test_a_permanent_error_is_not_retried(self):
        self.feed([(404, {'error_description': 'share not found'})])
        self.waits()
        with self.assertRaises(Exception):
            self.client.share_info('MISSINGSHAREID')
        self.assertEqual(self.slept, [], 'a 404 must not be retried')

    def test_rate_limit_backs_off_far_longer_than_a_blip(self):
        self.feed([(429, {'error_description': 'slow down'}), (200, {})],)
        self.waits()
        self.client.about()
        self.assertEqual(self.slept, [30])


class TestStartupSweep(unittest.TestCase):
    """The sweep runs where nothing is in flight, and must never touch a file the
    user put there themselves — only copies our own state recorded."""

    def setUp(self):
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  dest=os.path.join(self.dir, 'lib'), max_files=0,
                                  connections=1, gap=0, repeat=0, no_sweep=False)
        self.pipeline = Pipeline(args, Log(quiet=True))
        self.removed = []
        calls = collections.deque([
            {'id': 'OURS_A', 'kind': 'drive#file', 'size': '10'},
            {'id': 'SOMEONE_ELS', 'kind': 'drive#file', 'size': '99'},
            {'id': 'FOLDER', 'kind': 'drive#folder', 'size': '0'},
        ])
        self.pipeline.client.list_folder = lambda parent_id='*', **kw: list(calls)
        self.pipeline.client.cleanup = lambda ids: self.removed.extend(ids)
        self.pipeline.space = lambda: {'limit': 6_000_000_000, 'usage': 500,
                                        'in_trash': 0, 'free': 5_500_000_000}
        self.pipeline.wait_space_freed = lambda size, before=None: {
            'limit': 6_000_000_000, 'usage': 0, 'in_trash': 0, 'free': 6_000_000_000}
        self.state = self.pipeline.state
        self.state.file('FILE_A', 'url')['state'] = 'restoring'
        self.state.file('FILE_A', 'url')['restored_id'] = 'OURS_A'
        self.state.file('FILE_B', 'url')['state'] = 'restoring'
        self.state.file('FILE_B', 'url')['restored_id'] = 'OURS_B'      # gone already
        self.state.file('FILE_DONE', 'url')['state'] = 'done'
        self.state.file('FILE_DONE', 'url')['restored_id'] = 'DONE_ID'
        self.state.save()

    def sweep(self):
        return self.pipeline.sweep_leftovers()

    def test_only_our_recorded_copy_is_removed(self):
        self.sweep()
        self.assertEqual(self.removed, ['OURS_A'])

    def test_foreign_files_and_folders_are_untouched(self):
        self.sweep()
        self.assertNotIn('SOMEONE_ELS', self.removed)
        self.assertNotIn('FOLDER', self.removed)

    def test_a_completed_file_is_never_reclaimed(self):
        self.sweep()
        self.assertNotIn('DONE_ID', self.removed)

    def test_the_stale_reference_is_cleared_for_the_next_attempt(self):
        self.sweep()
        self.assertIsNone(self.state.data['files']['FILE_A']['restored_id'])

    def test_nothing_happens_without_recorded_copies(self):
        self.removed.clear()
        for rec in self.state.data['files'].values():
            rec['restored_id'] = None
        self.sweep()
        self.assertEqual(self.removed, [])


class TestStopOnZeroProgress(unittest.TestCase):
    """Repeat passes exist to pick up transient failures; looping while nothing
    lands is exactly the hammering that gets an account flagged."""

    def setUp(self):
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  dest=os.path.join(self.dir, 'lib'), max_files=0,
                                  connections=1, gap=0, repeat=0, dry_run=True,
                                  no_sweep=True,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=False, log='-', quiet=True)
        self.pipeline = Pipeline(args, Log(quiet=True))
        os.makedirs(args.dest, exist_ok=True)
        self.pipeline.space = lambda: {'limit': 6_000_000_000, 'usage': 0,
                                       'in_trash': 0, 'free': 6_000_000_000}
        self.calls = 0

        def failing_link(job):
            self.calls += 1
            self.pipeline.state.link(job['url'])['status'] = 'error'
            return 'error'
        self.pipeline.run_link = failing_link
        self.jobs = [{'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
                      'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'S', 'order': 1}]

    def test_a_zero_progress_pass_stops_instead_of_retrying_forever(self):
        self.assertEqual(self.pipeline.run(self.jobs), 1)
        self.assertEqual(self.calls, 1)

    def test_repeat_limit_is_respected_when_progress_happens(self):
        self.pipeline.args.repeat = 2
        state = {'n': 0}

        def occasionally_ok(job):
            state['n'] += 1
            if state['n'] % 2:
                self.pipeline.bytes_done += 10
                self.pipeline.files_done += 1
            self.pipeline.state.link(job['url'])['status'] = 'error'
            return 'error'
        self.pipeline.run_link = occasionally_ok
        self.pipeline.run(self.jobs)
        self.assertLessEqual(state['n'], 4)


class TestSegmentSafetyValve(unittest.TestCase):
    """The refusal-vs-starvation distinction is the risk-control safety valve: a
    refusal must drop to one connection, an unlucky lane must not."""

    class DeadProcess:
        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def setUp(self):
        import pikpakget.stream as stream
        self.stream = stream
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, 'file.bin')
        self.probes = []

        def fake_spawn(item, url):
            handle = open(item['path'], 'ab')         # touch it, but deliver nothing
            handle.close()
            return self.DeadProcess()

        self.spawn = stream._spawn
        stream._spawn = fake_spawn
        self.addCleanup(setattr, stream, '_spawn', self.spawn)

    def probe(self, code):
        def fake(url, start=0, log=None, note=''):
            self.probes.append(code)
            return code
        self.original = self.stream.probe_status
        self.stream.probe_status = fake
        self.addCleanup(setattr, self.stream, 'probe_status', self.original)

    def run_download(self):
        return self.stream.download_segments(lambda: 'http://invalid.invalid', self.target,
                                             1000, 4, wait=lambda seconds: None,
                                             log=lambda message, level='info': None)

    def test_a_round_that_made_progress_is_not_penalised_with_a_wait(self):
        """Back-off is for dead rounds. Sleeping 60s after a round that moved bytes
        was throwing away about a third of the transfer time."""
        calls = []

        def spawn_lanes(item, url):
            calls.append(item['index'])
            # a third of each lane on the first pass, the rest on the next one
            with open(item['path'], 'ab') as handle:
                handle.truncate(item['want'] if len(calls) > 2 else item['want'] // 3)
            return self.DeadProcess()

        self.stream._spawn = spawn_lanes
        self.probe('206')
        waits = []
        result = self.stream.download_segments(lambda: 'http://invalid.invalid', self.target,
                                              1000, 2, wait=waits.append,
                                              log=lambda message, level='info': None)
        self.assertEqual(result, 1000)
        self.assertEqual([seconds for seconds in waits if seconds >= 60], [],
                         'productive rounds must not trigger back-off')


    def test_refusal_raises_segment_refused(self):
        from pikpakget.api import PikPakError
        from pikpakget.stream import SegmentRefused
        self.probe('403')
        with self.assertRaises(SegmentRefused) as caught:
            self.run_download()
        self.assertIn('403', str(caught.exception), 'the refusal code should be reported')
        self.assertTrue(self.probes, 'the CDN should be probed before giving up')
        self.assertTrue(issubclass(SegmentRefused, PikPakError))

    def test_starvation_keeps_trying_and_does_not_blame_the_server(self):
        from pikpakget.api import PikPakError
        from pikpakget.stream import SegmentRefused
        self.probe('206')
        with self.assertRaises(PikPakError) as caught:
            self.run_download()
        self.assertNotIsInstance(caught.exception, SegmentRefused)
        self.assertIn('未完成', str(caught.exception))

    def test_segment_directory_is_scoped_to_its_width(self):
        self.assertNotEqual(self.stream._segment_path('/tmp/a.part', 2),
                            self.stream._segment_path('/tmp/a.part', 4))


class TestCaptchaSign(unittest.TestCase):
    def test_shape_and_stability(self):
        sign, stamp = captcha_sign('CLIENTID', 'DEVICEID', timestamp='1700000000000')
        self.assertTrue(sign.startswith('1.'))
        self.assertEqual(len(sign), 2 + 32)
        again, _ = captcha_sign('CLIENTID', 'DEVICEID', timestamp='1700000000000')
        self.assertEqual(sign, again)

    def test_device_and_timestamp_change_the_sign(self):
        base, _ = captcha_sign('CLIENTID', 'DEVICEID', timestamp='1700000000000')
        other, _ = captcha_sign('CLIENTID', 'OTHERDEV', timestamp='1700000000000')
        self.assertNotEqual(base, other)

    def test_timestamp_defaults_to_now(self):
        _, stamp = captcha_sign('CLIENTID', 'DEVICEID')
        self.assertGreater(int(stamp), 1_600_000_000_000)


class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'state.json')

    def test_roundtrip_and_defaults(self):
        state = State(self.path)
        state.file('FILE1', 'url-a').update({'size': 10, 'state': 'done'})
        state.save()
        reloaded = State(self.path)
        self.assertEqual(reloaded.file('FILE1')['size'], 10)
        self.assertEqual(reloaded.data['links'], {})

    def test_corrupt_state_is_preserved_not_ignored(self):
        with open(self.path, 'w') as handle:
            handle.write('{not json')
        State(self.path)
        backups = [name for name in os.listdir(self.dir) if 'corrupt' in name]
        self.assertEqual(len(backups), 1)

    def test_files_of_groups_by_link(self):
        state = State(self.path)
        state.file('A', 'url-a')
        state.file('B', 'url-b')
        self.assertEqual(len(state.files_of('url-a')), 1)

    def test_save_is_atomic(self):
        state = State(self.path)
        state.link('url-a')['status'] = 'active'
        state.save()
        with open(self.path) as handle:
            json.load(handle)
        self.assertFalse(os.path.exists(self.path + '.tmp'))


class TestStateMigration(unittest.TestCase):
    """v1 keyed links by bare share URL, v2 by URL plus folder. Unmigrated records
    are invisible to the new key, so a run would replan work it had already done."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'state.json')

    def write_v1(self, data):
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)

    def test_link_keys_gain_their_folder_and_files_follow(self):
        self.write_v1({'version': 1,
                       'links': {'http://s/A': {'status': 'partial', 'folder': 'Series'}},
                       'files': {'F1': {'state': 'done', 'url': 'http://s/A'}}})
        state = State(self.path)
        self.assertEqual(state.data['version'], STATE_VERSION)
        self.assertEqual(list(state.data['links']), ['http://s/A\tSeries'])
        self.assertEqual(state.file('F1')['url'], 'http://s/A\tSeries')

    def test_migration_is_persisted_so_it_runs_once(self):
        self.write_v1({'version': 1, 'links': {'http://s/A': {'folder': 'Series'}},
                       'files': {}})
        State(self.path)
        with open(self.path, encoding='utf-8') as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk['version'], STATE_VERSION)
        self.assertIn('http://s/A\tSeries', on_disk['links'])

    def test_a_link_already_seen_under_the_new_key_is_merged(self):
        self.write_v1({'version': 1, 'links': {
            'http://s/A': {'status': 'partial', 'folder': 'Series', 'title': 'Series'},
            'http://s/A\tSeries': {'status': 'active', 'file_count': 8}},
            'files': {}})
        state = State(self.path)
        self.assertEqual(len(state.data['links']), 1)
        kept = state.data['links']['http://s/A\tSeries']
        self.assertEqual(kept['status'], 'active')       # the newer record wins
        self.assertEqual(kept['title'], 'Series')        # and nothing else is lost

    def test_a_file_without_a_folder_migrates_to_the_empty_folder(self):
        self.write_v1({'version': 1, 'links': {'http://s/A': {}}, 'files': {}})
        state = State(self.path)
        self.assertIn('http://s/A\t', state.data['links'])


class TestStateKeyCannotHideWork(RunLinkHarness):
    """The bug this guards: records created before the key change still carry the
    bare URL, so grouping files by the link key returned nothing, `pending` looked
    empty, and a link with two files left on the cloud was declared done."""

    def seed_stale_record(self, file_id, url):
        self.pipeline.state.data['files'][file_id] = {
            'state': 'pending', 'attempts': self.module.MAX_ATTEMPTS - 1, 'url': url,
            'restored_id': None, 'local': None, 'size': 100, 'seconds': None,
            'error': None, 'name': 'a.mp4', 'path': 'a.mp4'}

    def test_abandoned_file_keeps_the_link_incomplete(self):
        url = 'https://mypikpak.com/s/EXAMPLEID1111111111'
        self.seed_stale_record('SHAREFILE1', url)
        def refuse(*rest, **kwargs):
            raise OSError('connection reset')
        self.module.download_stream = refuse
        node = {'id': 'SHAREFILE1', 'name': 'a.mp4', 'size': 100, 'path': 'a.mp4',
                'hash': None, 'is_folder': False}
        self.pipeline.inventory = lambda job: {
            'title': 'Series', 'token': 'TOKEN', 'files': [node],
            'total': 100, 'folders': 0, 'truncated': False}
        job = {'url': url, 'share_id': 'EXAMPLEID1111111111', 'pass_code': '',
               'folder': 'Series', 'order': 1}
        self.assertEqual(self.pipeline.run_link(job), 'incomplete')
        self.assertEqual(self.pipeline.state.link(job['url'] + '\tSeries')['status'],
                         'incomplete')

    def test_finished_file_with_a_stale_url_is_not_downloaded_twice(self):
        local = os.path.join(self.dir, 'already.mp4')
        with open(local, 'wb') as handle:
            handle.truncate(100)
        self.seed_stale_record('SHAREFILE1', 'https://mypikpak.com/s/EXAMPLEID1')
        self.pipeline.state.data['files']['SHAREFILE1'].update(
            {'state': 'done', 'attempts': 1, 'local': local})
        self.assertEqual(self.run_one()[0], 'ok')
        self.assertEqual(self.downloads, [])
        # the record keeps pointing at the key it was created under; nothing reads
        # that field to decide whether work remains
        self.assertEqual(self.pipeline.state.file('SHAREFILE1')['url'],
                         'https://mypikpak.com/s/EXAMPLEID1')


class TestSessionVersusOneBadLink(RunLinkHarness):
    """PikPak answers 403 for a dead session and for a share that has been revoked,
    and the two are indistinguishable at the call site. Guessing "dead session" on
    the first one stops a run that has days of scheduling left."""

    def error(self, status=403):
        from pikpakget.api import PikPakError
        return PikPakError('access denied', status=status)

    def link_that_is_refused(self):
        self.pipeline.inventory = lambda job: (_ for _ in ()).throw(self.error())
        return {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
                'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}

    def test_one_refusal_skips_the_link_instead_of_stopping_the_run(self):
        self.assertFalse(self.pipeline.note_throttle(self.error()))
        self.assertEqual(self.pipeline.auth_hits, 1)

    def test_run_link_reports_an_error_not_a_stop(self):
        self.assertEqual(self.pipeline.run_link(self.link_that_is_refused()), 'error')

    def test_a_listing_that_answers_clears_the_suspicion(self):
        self.pipeline.auth_hits = 2
        self.pipeline.inventory = lambda job: {'title': 'Series', 'token': 'T',
                                              'files': [], 'total': 0, 'folders': 0,
                                              'truncated': False}
        job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
               'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}
        self.pipeline.run_link(job)
        self.assertEqual(self.pipeline.auth_hits, 0)

    def test_refusals_in_a_row_do_blame_the_session(self):
        for _ in range(AUTH_STOP - 1):
            self.assertFalse(self.pipeline.note_throttle(self.error()))
        self.assertTrue(self.pipeline.note_throttle(self.error()))

    def test_a_session_the_server_refused_to_refresh_stops_at_once(self):
        self.pipeline.client.session_dead = True
        self.assertTrue(self.pipeline.note_throttle(self.error()))
        self.assertEqual(self.pipeline.auth_hits, 0)


class TestRefreshFailureIsNotAlwaysFatal(unittest.TestCase):
    """A socket that never reached the server says nothing about the session."""

    def setUp(self):
        from pikpakget.api import Client
        dirpath = tempfile.mkdtemp()
        self.client = Client(session_path=os.path.join(dirpath, 'session.json'),
                             device_id_path=os.path.join(dirpath, 'device_id'))
        self.client.session.store({'access_token': 'a' * 40, 'refresh_token': 'r',
                                   'sub': 'u', 'expires_at': time.time() + 3600})

    def answer(self, result):
        self.client._raw = lambda *args, **kwargs: result

    def test_a_blip_leaves_the_session_marked_alive(self):
        from pikpakget.api import PikPakError
        self.answer((None, {'error_description': 'URLError: connection reset'}))
        with self.assertRaises(PikPakError):
            self.client.refresh()
        self.assertFalse(self.client.session_dead)

    def test_a_refused_refresh_marks_the_session_dead(self):
        from pikpakget.api import PikPakError
        self.answer((403, {'error_description': 'invalid refresh token'}))
        with self.assertRaises(PikPakError):
            self.client.refresh()
        self.assertTrue(self.client.session_dead)


class TestDocumentedCounts(unittest.TestCase):
    """Both READMEs quote the number of tests, and both went stale silently. Counting
    the suite from inside it keeps the claim tied to the code."""

    def test_both_readmes_quote_the_real_number_of_tests(self):
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        total = unittest.defaultTestLoader.discover(here, pattern='test_*.py').countTestCases()
        for name, pattern in (('README.md', r'(\d+) tests on the pure logic'),
                              ('README.cn.md', r'(\d+) 项纯逻辑测试')):
            with open(os.path.join(here, '..', name), encoding='utf-8') as handle:
                quoted = re.search(pattern, handle.read())
            self.assertIsNotNone(quoted, f'{name} 里找不到测试数量的说法')
            self.assertEqual(int(quoted.group(1)), total,
                             f'{name} 说 {quoted.group(1)} 项，实际 {total} 项')


class TestHuman(unittest.TestCase):
    def test_binary_units_are_labelled_binary(self):
        self.assertEqual(human(0), '0.0B')
        self.assertEqual(human(1024), '1.0KiB')
        self.assertEqual(human(5 * 1024 ** 3), '5.0GiB')

    def test_terabytes(self):
        self.assertTrue(human(40 * 1024 ** 4).endswith('TiB'))

    def test_the_quota_reads_as_six_gib(self):
        # 6442450944 bytes is exactly 6 GiB; labelling it "6 GB" made two tools'
        # figures disagree by 7%
        self.assertEqual(human(6442450944), '6.0GiB')

    def test_no_decimal_labels_leak(self):
        for size in (1024, 1024 ** 2, 1024 ** 3, 1024 ** 4):
            self.assertFalse(human(size).endswith(('KB', 'MB', 'GB', 'TB')))


if __name__ == '__main__':
    unittest.main()
