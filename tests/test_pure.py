"""Tests for the pure logic. Synthetic data only — no live account, no real links."""
import json
import os
import tempfile
import unittest

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
        json.load(open(self.path))
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
