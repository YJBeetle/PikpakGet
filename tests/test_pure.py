"""Tests for the pure logic. Synthetic data only — no live account, no real links."""
import json
import os
import tempfile
import unittest

import argparse

from pikpakget.api import captcha_sign, parse_share_url
from pikpakget.pipeline import State, human, load_folder_map, read_links, safe_name
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
        name = safe_name('../../etc/passwd')
        self.assertNotIn('/', name)
        self.assertNotIn('..', name)

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

    def test_dots_only_name_is_neutralised(self):
        self.assertNotEqual(safe_name('..'), '')


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


class TestNameCollisions(unittest.TestCase):
    """Remote names come from someone else's folders, so collisions are the norm
    and a silent overwrite would be data loss."""

    def setUp(self):
        import argparse
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

    def test_overshot_segment_is_dropped_and_reported(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1500)                    # bytes from the wrong offset
        dropped = self.stream.discard_overshot([self.item])
        self.assertEqual(dropped, [0])
        self.assertFalse(os.path.exists(self.item['path']))

    def test_exact_segment_survives(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1000)
        self.assertEqual(self.stream.discard_overshot([self.item]), [])
        self.assertTrue(os.path.exists(self.item['path']))


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

    def test_overshot_segment_is_dropped_not_truncated(self):
        good = self.item(0, 0, 499, have=500)
        bad = self.item(1, 500, 999, have=560)             # bytes from a bad offset
        dropped = self.stream.discard_overshot([good, bad])
        self.assertEqual(dropped, [1])
        self.assertTrue(os.path.exists(good['path']))
        self.assertFalse(os.path.exists(bad['path']))

    def test_range_is_capped_so_a_ignoring_cannot_double_the_file(self):
        command = self.stream.build_command(self.item(2, 200, 299), 'http://host/url')
        self.assertIn('--max-filesize', command)
        self.assertEqual(command[command.index('--max-filesize') + 1], '100')


class TestStopOnZeroProgress(unittest.TestCase):
    """Repeat passes exist to pick up transient failures; looping while nothing
    lands is exactly the hammering that gets an account flagged."""

    def setUp(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  dest=os.path.join(self.dir, 'lib'), max_files=0,
                                  connections=1, gap=0, repeat=0, dry_run=True,
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

    def test_refusal_raises_segment_refused(self):
        from pikpakget.api import PikPakError
        from pikpakget.stream import SegmentRefused
        self.probe('403')
        with self.assertRaises(SegmentRefused) as caught:
            self.run_download()
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


class TestHuman(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human(0), '0.0B')
        self.assertEqual(human(1024), '1.0KB')
        self.assertEqual(human(5 * 1024 ** 3), '5.0GB')

    def test_terabytes(self):
        self.assertTrue(human(40 * 1024 ** 4).endswith('TB'))


if __name__ == '__main__':
    unittest.main()
