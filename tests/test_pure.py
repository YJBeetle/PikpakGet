"""Tests for the pure logic. Synthetic data only — no live account, no real links."""
import collections
import hashlib
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
        self.module.shutil = types.SimpleNamespace(disk_usage=lambda path: plenty,
                                                  which=real_shutil.which)
        self.pipeline.client.download_url = lambda fid: ('http://host/file', {})
        self.addCleanup(setattr, pipeline_module, 'download_stream', pipeline_module.download_stream)
        self.downloads = []

    def fake_stream(self, url, target, expected=0, resume=0, *rest, **kwargs):
        self.downloads.append((target, expected))
        with open(target, 'wb') as handle:
            handle.truncate(expected)
        return expected

    def run_one(self, name='a.mp4', size=100, path=None, digest=None):
        self.module.download_stream = self.fake_stream
        node = {'id': 'SHAREFILE1', 'name': name, 'size': size,
                'path': path or name, 'hash': digest, 'is_folder': False}
        self.pipeline.inventory = lambda job: {
            'title': 'Series', 'token': 'TOKEN', 'files': [node],
            'total': size, 'folders': 0, 'truncated': False}
        job = {'url': 'https://mypikpak.com/s/EXAMPLEID1111111111', 'share_id':
               'EXAMPLEID1111111111', 'pass_code': '', 'folder': 'Series', 'order': 1}
        return self.pipeline.run_link(job), node


def fold(content, piece):
    """PikPak's hash rule, written out here so a passing implementation cannot be
    the same mistake twice over: sha1 over the concatenated sha1 of each block."""
    outer = hashlib.sha1()
    for offset in range(0, len(content), piece):
        outer.update(hashlib.sha1(content[offset:offset + piece]).digest())
    return outer.hexdigest().upper()


class TestContentHash(unittest.TestCase):
    """Reverse engineered from real files: the block size is whatever the uploader's
    client used — 1 MiB on most, 512 KiB on one."""

    def setUp(self):
        import pikpakget.stream as stream
        self.stream = stream
        self.dir = tempfile.mkdtemp()

    def write(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as handle:
            handle.write(content)
        return path

    def test_a_file_shorter_than_one_block(self):
        content = b'hello pikpak' * 7
        self.assertEqual(self.stream.content_hash(self.write('small.bin', content), 1 << 20),
                         fold(content, 1 << 20))

    def test_whole_blocks_plus_a_partial_tail(self):
        content = bytes(range(256)) * 20000            # 5 MB + 192 KB tail
        path = self.write('mid.bin', content)
        for piece in (1 << 20, 512 << 10, 4 << 20):
            self.assertEqual(self.stream.content_hash(path, piece), fold(content, piece))

    def test_verify_reports_the_block_size_that_reproduces_the_server(self):
        content = os.urandom(1 << 20) + b'x' * 1000
        path = self.write('fold.bin', content)
        self.assertEqual(self.stream.verify_content(path, fold(content, 512 << 10)), 512 << 10)
        self.assertEqual(self.stream.verify_content(path, fold(content, 1 << 20)), 1 << 20)

    def test_a_shifted_middle_with_the_right_length_does_not_verify(self):
        content = b'a' * (1 << 20) + b'b' * 8192
        good = fold(content, 1 << 20)
        damaged = content[:4096] + content[4096 + 1024:] + content[-1024:]
        self.assertEqual(len(damaged), len(content), 'the byte count check stays green')
        self.assertIsNone(self.stream.verify_content(self.write('bad.bin', damaged), good))

    def test_nothing_to_check_is_not_a_failure(self):
        path = self.write('any.bin', b'bytes')
        self.assertIsNone(self.stream.verify_content(path, None))
        self.assertIsNone(self.stream.verify_content(path, ''))


class TestContentVerificationOnPromote(RunLinkHarness):
    """The last line of defense: a file can be exactly the right length and wrong
    inside, and only the server's hash can tell."""

    def test_a_file_that_reproduces_the_server_hash_is_kept(self):
        status, node = self.run_one(size=100, digest=fold(b'\0' * 100, 1 << 20))
        self.assertEqual(status, 'ok')
        self.assertTrue(os.path.exists(os.path.join(self.lib, 'Series', 'a.mp4')))

    def test_a_downloaded_file_remembers_the_block_size_it_verified_at(self):
        status, node = self.run_one(size=100, digest=fold(b'\0' * 100, 1 << 20))
        self.assertEqual(status, 'ok')
        self.assertEqual(self.pipeline.state.file('SHAREFILE1')['hash_piece'], 1 << 20)

    def test_a_wrong_file_is_dropped_and_never_marked_done(self):
        status, node = self.run_one(size=100, digest='0' * 40)
        self.assertEqual(status, 'retry')
        final = os.path.join(self.lib, 'Series', 'a.mp4')
        self.assertFalse(os.path.exists(final))
        self.assertFalse(os.path.exists(final + '.part'), 'a retry must not resume bad bytes')
        record = self.pipeline.state.file('SHAREFILE1')
        self.assertEqual(record['state'], 'retry')
        self.assertIn('内容 hash', record['error'])

    def test_a_refetch_replaces_the_quarantine_and_clears_the_reference(self):
        doomed = os.path.join(self.lib, 'Series', 'a.mp4.unverified')
        os.makedirs(os.path.dirname(doomed), exist_ok=True)
        with open(doomed, 'wb') as handle:
            handle.write(b'junk' * 25)
        home = os.path.join(self.lib, 'Series', 'a.mp4')
        self.pipeline.state.file('SHAREFILE1',
                                 'https://mypikpak.com/s/EXAMPLEID1111111111\tSeries').update(
            {'state': 'pending', 'local': home, 'home': home, 'quarantined': doomed})
        self.assertEqual(self.run_one(size=100, digest=fold(b'\0' * 100, 1 << 20))[0], 'ok')
        self.assertTrue(os.path.exists(home))
        self.assertFalse(os.path.exists(doomed), 'the copy nobody could vouch for is gone')
        self.assertIsNone(self.pipeline.state.file('SHAREFILE1')['quarantined'])

    def test_a_file_without_a_server_hash_still_lands(self):
        status, node = self.run_one(size=100)
        self.assertEqual(status, 'ok')
        self.assertTrue(os.path.exists(os.path.join(self.lib, 'Series', 'a.mp4')))


class TestVerifyMode(RunLinkHarness):
    """`--verify` re-checks the library with the hash the share still reports. It has
    to tell "corrupt" apart from "the user moved this file out" — the second one is
    the normal life of a library."""

    def job(self):
        url = 'https://mypikpak.com/s/EXAMPLEID1111111111'
        return {'url': url, 'share_id': 'EXAMPLEID1111111111', 'pass_code': '',
                'folder': 'Series', 'order': 1, 'key': url + '\tSeries'}

    def seed_done(self, file_id, name, content, digest, local=None):
        folder = os.path.join(self.lib, 'Series')
        os.makedirs(folder, exist_ok=True)
        path = local or os.path.join(folder, name)
        if local is None:
            with open(path, 'wb') as handle:
                handle.write(content)
        record = self.pipeline.state.file(file_id, self.job()['key'])
        record.update({'state': 'done', 'local': path, 'size': len(content)})
        node = {'id': file_id, 'name': name, 'size': len(content), 'path': name,
                'hash': digest, 'is_folder': False}
        self.node = node
        job = self.job()
        self.pipeline.inventory = lambda ignored: {
            'title': 'Series', 'token': 'T', 'files': [node], 'total': len(content),
            'folders': 0, 'truncated': False}
        return job

    def test_a_library_that_hashes_out_passes_and_remembers_the_block_size(self):
        # 600000 bytes: long enough that the 1 MiB and 512 KiB folds differ, so a
        # reported block size means the candidate loop really walked the list
        content = b'pikpak' * 100000
        job = self.seed_done('F1', 'good.mp4', content, fold(content, 512 << 10))
        self.assertEqual(self.pipeline.verify([job]), 0)
        self.assertEqual(self.pipeline.state.file('F1')['hash_piece'], 512 << 10)

    def test_right_length_wrong_bytes_is_quarantined_not_deleted(self):
        content = b'a' * 4096 + b'b' * 2048
        job = self.seed_done('F1', 'bad.mp4', content,
                             fold(b'a' * 4096 + b'c' * 2048, 1 << 20))
        self.assertEqual(self.pipeline.verify([job]), 1)
        doomed = os.path.join(self.lib, 'Series', 'bad.mp4.unverified')
        self.assertTrue(os.path.exists(doomed), 'the bytes survive as evidence')
        self.assertFalse(os.path.exists(os.path.join(self.lib, 'Series', 'bad.mp4')))
        record = self.pipeline.state.file('F1')
        self.assertEqual(record['state'], 'pending', 'so the next download run replaces it')
        # the record keeps pointing at *both* copies: a sweep that renamed a file and
        # then dropped the reference left orphans nobody could find again
        self.assertEqual(record['local'], os.path.join(self.lib, 'Series', 'bad.mp4'))
        self.assertEqual(record['quarantined'], doomed)

    def test_a_quarantined_file_that_now_hashes_out_is_moved_back(self):
        # the block-size candidate list will grow; a file parked because of a guess
        # should come home on its own once the guess covers it
        content = b'y' * 3000
        doomed = os.path.join(self.lib, 'Series', 'fixed.mp4.unverified')
        home = os.path.join(self.lib, 'Series', 'fixed.mp4')
        job = self.seed_done('F1', 'fixed.mp4', content, fold(content, 1 << 20), local=doomed)
        with open(doomed, 'wb') as handle:
            handle.write(content)
        self.pipeline.state.file('F1').update({'state': 'unverified', 'home': home,
                                               'quarantined': doomed})
        self.assertEqual(self.pipeline.verify([job]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.lib, 'Series', 'fixed.mp4')))
        self.assertFalse(os.path.exists(doomed))
        self.assertEqual(self.pipeline.state.file('F1')['state'], 'done')

    def test_one_dead_share_does_not_stop_the_sweep(self):
        content = b'x' * 5000
        good = self.seed_done('F1', 'good.mp4', content, fold(content, 1 << 20))
        dead = {'url': 'https://mypikpak.com/s/DEADDEADDEADDEAD0000', 'share_id': 'DEAD',
                'pass_code': '', 'folder': 'Dead', 'order': 2}
        node = self.node

        def inventory(job):
            if job['url'] == dead['url']:
                from pikpakget.api import PikPakError
                raise PikPakError('分享已失效', status=403)
            return {'title': 'Series', 'token': 'T', 'files': [node], 'total': len(content),
                    'folders': 0, 'truncated': False}
        self.pipeline.inventory = inventory
        self.assertEqual(self.pipeline.verify([dead, good]), 0)
        self.assertEqual(self.pipeline.state.file('F1')['hash_piece'], 1 << 20,
                         'the healthy link was still checked')

    def test_a_moved_file_is_not_called_corrupt(self):
        job = self.seed_done('F1', 'gone.mp4', b'x' * 10, fold(b'x' * 10, 1 << 20),
                             local=os.path.join(self.lib, 'Series', 'moved-away.mp4'))
        self.assertEqual(self.pipeline.verify([job]), 0)

    def test_a_share_that_gives_no_hash_cannot_be_checked(self):
        job = self.seed_done('F1', 'nohash.mp4', b'y' * 10, None)
        self.assertEqual(self.pipeline.verify([job]), 0)


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


class TestPipelineStartup(unittest.TestCase):
    """Building the pipeline over a state that already has finished files is the very
    first thing every resumed run does — and it once died on an attribute that was
    initialised after the loop reading it, which no fixture had ever covered."""

    def make_state(self, dirpath, local):
        from pikpakget.pipeline import State
        state_dir = os.path.join(dirpath, '.state')
        os.makedirs(state_dir)
        state = State(os.path.join(state_dir, 'state.json'))
        with open(local, 'wb') as handle:
            handle.write(b'x' * 10)
        state.file('F1', 'https://mypikpak.com/s/EXAMPLEID1\tSeries').update(
            {'state': 'done', 'local': local, 'size': 10, 'path': 'done.mp4'})
        state.save()
        return state_dir

    def test_a_state_with_finished_files_builds_a_pipeline(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        dirpath = tempfile.mkdtemp()
        library = os.path.join(dirpath, 'lib', 'Series')
        os.makedirs(library)
        local = os.path.join(library, 'done.mp4')
        state_dir = self.make_state(dirpath, local)
        args = argparse.Namespace(state_dir=state_dir, dest=os.path.join(dirpath, 'lib'),
                                  max_files=0, connections=1, gap=0, repeat=0, dry_run=False,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=True, no_sweep=True)
        pipeline = Pipeline(args, Log(quiet=True))
        self.assertIsInstance(pipeline._case_folded, bool)
        # the map holds the *source path inside the share*, which is what a later
        # same-name file is compared against to decide whether to rename
        key = (library, 'done.mp4'.casefold() if pipeline._case_folded else 'done.mp4')
        self.assertEqual(pipeline.used_names[key], 'done.mp4')


class TestStatusWithoutProgress(unittest.TestCase):
    """`--status` on a machine with no records printed a bare table header, which is
    the single most confusing thing an empty report can do."""

    def setUp(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'),
                                  dest=os.path.join(self.dir, 'lib'), max_files=0,
                                  connections=1, gap=0, repeat=0, dry_run=False,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=True, no_sweep=True)
        os.makedirs(args.dest)
        self.pipeline = Pipeline(args, Log(quiet=True))

    def test_nothing_is_said_as_nothing_not_as_an_empty_table(self):
        import contextlib
        import io
        from pikpakget.api import PikPakError
        self.pipeline.space = lambda: (_ for _ in ()).throw(
            PikPakError('还没有登录', action='session'))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.pipeline.status()
        text = buffer.getvalue()
        self.assertEqual(code, 1, 'a report that could not reach the account is not clean')
        self.assertNotIn('folder                    status', text)
        self.assertIn('没有任何进度记录', text)

    def test_a_missing_dest_is_measured_on_its_volume_not_crashed_on(self):
        """`--status` on a machine that never downloaded anything died in statvfs.

        The report is read-only, so it has no business creating `--dest` to answer a
        question about free space."""
        import contextlib
        import io
        import shutil
        self.pipeline.space = lambda: {'limit': 6442450944, 'usage': 0, 'in_trash': 0,
                                       'free': 6442450944}
        shutil.rmtree(self.pipeline.args.dest)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.pipeline.status()
        self.assertEqual(code, 0, 'a missing downloads folder is not an error')
        self.assertIn('本地剩余', buffer.getvalue())
        self.assertFalse(os.path.exists(self.pipeline.args.dest),
                         'measuring the volume must not create the directory')

    def test_quotas_are_still_counted_when_the_account_answers(self):
        import contextlib
        import io
        self.pipeline.space = lambda: {'limit': 6442450944, 'usage': 0, 'in_trash': 0,
                                       'free': 6442450944}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(self.pipeline.status(), 0)
        self.assertIn('云盘', buffer.getvalue())


class TestLoginNeedsNoArgument(unittest.TestCase):
    """`--login` with no value is the first thing anyone types; argparse answered it
    with an English usage dump, which is not a reply to a question about the account."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def sign_in(self, account, password):
            LoginCalls.append((account, password))

    def setUp(self):
        import unittest.mock
        global LoginCalls
        LoginCalls = []
        import pikpakget.cli as cli
        self.cli = cli
        self.patch = unittest.mock.patch.object(cli, 'Client', TestLoginNeedsNoArgument.FakeClient)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.getpass = unittest.mock.patch.object(cli.getpass, 'getpass', lambda prompt: 'pw')
        self.getpass.start()
        self.addCleanup(self.getpass.stop)

    def fake_stdin(self, tty):
        import types
        return types.SimpleNamespace(isatty=lambda: tty, read=lambda: 'pw\n')

    def test_a_tty_gets_asked_for_the_account(self):
        import sys
        import unittest.mock
        with unittest.mock.patch.object(sys, 'stdin', self.fake_stdin(True)), \
                unittest.mock.patch('builtins.input', lambda prompt: 'typed@example.com'):
            self.assertEqual(self.cli.main(['--login', '--state-dir', tempfile.mkdtemp(),
                                            '--dest', tempfile.mkdtemp()]), 0)
        self.assertEqual(LoginCalls, [('typed@example.com', 'pw')])

    def test_a_scripted_call_without_a_terminal_says_so(self):
        import contextlib
        import io
        import sys
        import unittest.mock
        buffer = io.StringIO()
        with unittest.mock.patch.object(sys, 'stdin', self.fake_stdin(False)), \
                contextlib.redirect_stderr(buffer):
            code = self.cli.main(['--login', '--state-dir', tempfile.mkdtemp(),
                                  '--dest', tempfile.mkdtemp()])
        self.assertEqual(code, 2)
        text = buffer.getvalue()
        self.assertIn('需要账号', text)
        self.assertNotIn('usage:', text, 'no argparse dump')

    def test_an_account_on_the_command_line_still_works(self):
        self.assertEqual(self.cli.main(['--login', 'given@example.com', '--state-dir',
                                        tempfile.mkdtemp(), '--dest', tempfile.mkdtemp()]), 0)
        self.assertEqual(LoginCalls, [('given@example.com', 'pw')])


class TestLogCloses(unittest.TestCase):
    """main() opens the log and now closes it on every path; a library caller keeps
    writing to the one it made, so closing must be safe twice and writing after close
    must not be silent."""

    def test_close_is_idempotent_and_writing_after_it_is_still_printed(self):
        import contextlib
        import io
        from pikpakget.pipeline import Log
        path = os.path.join(tempfile.mkdtemp(), 'sub', 'grab.log')
        log = Log(path)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            log('一条')
        log.close()
        log.close()
        with contextlib.redirect_stdout(buffer):
            log('两条')
        self.assertIn('两条', buffer.getvalue())
        with open(path, encoding='utf-8') as handle:
            self.assertIn('一条', handle.read())


class TestErrorsAreReadable(unittest.TestCase):
    """`--whoami` without a session was a wall of traceback. A refused command is
    information for the user; only a bug in here is information for me."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_missing_session_is_one_line_not_a_traceback(self):
        import contextlib
        import io
        import pikpakget.cli as cli
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = cli.main(['--whoami', '--state-dir', os.path.join(self.dir, 'nostate'),
                             '--dest', self.dir])
        text = buffer.getvalue()
        self.assertEqual(code, 2)
        self.assertNotIn('Traceback', text)
        self.assertEqual(text.count('错误：'), 1, text)
        self.assertIn('nostate/session.json', text, 'it must say which file it looked in')

    def test_a_local_failure_does_not_report_an_http_status(self):
        from pikpakget.api import PikPakError
        plain = PikPakError('还没有登录', action='session')
        self.assertNotIn('HTTP', str(plain))
        self.assertIn('action=session', str(plain))
        server = PikPakError('被限流', status=429, code=4003, action='restore')
        self.assertIn('HTTP 429, code=4003, action=restore', str(server))

    def test_a_lone_session_in_the_old_place_is_found(self):
        import pikpakget.cli as cli
        old = os.path.join(self.dir, 'repo')
        os.makedirs(os.path.join(old, '.pikpakget'))
        with open(os.path.join(old, '.pikpakget', 'session.json'), 'w') as handle:
            handle.write('{}')
        captured = []
        original = os.getcwd()
        os.chdir(old)
        self.addCleanup(os.chdir, original)
        args = argparse.Namespace(state_dir=os.path.join(self.dir, 'home'))
        cli._warn_about_legacy_state(args, lambda message, level='info': captured.append(message))
        self.assertEqual(len(captured), 1, captured)
        self.assertIn('登录会话', captured[0])


class TestStateFollowsTheUser(unittest.TestCase):
    """The state directory used to default to `./.pikpakget`, so a login from one
    directory was invisible to a run from another and read as a dropped session."""

    def test_the_default_state_directory_follows_home_not_cwd(self):
        import os as os_module
        import unittest.mock
        import pikpakget.cli as cli
        home = tempfile.mkdtemp()
        elsewhere = tempfile.mkdtemp()
        original = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, original)
        with unittest.mock.patch.dict(os_module.environ, {'HOME': home}):
            default = cli.build_parser().get_default('state_dir')
        self.assertEqual(default, os.path.join(home, '.pikpakget'))
        self.assertFalse(default.startswith(elsewhere))

    def test_progress_in_the_old_place_is_pointed_out(self):
        import pikpakget.cli as cli
        captured = []
        old = tempfile.mkdtemp()
        os.makedirs(os.path.join(old, '.pikpakget'))
        with open(os.path.join(old, '.pikpakget', 'state.json'), 'w') as handle:
            handle.write('{}')
        elsewhere = tempfile.mkdtemp()
        original = os.getcwd()
        os.chdir(old)
        self.addCleanup(os.chdir, original)
        args = argparse.Namespace(state_dir=elsewhere)
        cli._warn_about_legacy_state(args, lambda message, level='info': captured.append(message))
        self.assertEqual(len(captured), 1, captured)
        self.assertIn('--state-dir .pikpakget', captured[0])

    def test_no_notice_when_the_old_place_is_the_one_in_use(self):
        import pikpakget.cli as cli
        captured = []
        old = tempfile.mkdtemp()
        os.makedirs(os.path.join(old, '.pikpakget'))
        with open(os.path.join(old, '.pikpakget', 'state.json'), 'w') as handle:
            handle.write('{}')
        original = os.getcwd()
        os.chdir(old)
        self.addCleanup(os.chdir, original)
        args = argparse.Namespace(state_dir=os.path.join(old, '.pikpakget'))
        cli._warn_about_legacy_state(args, lambda message, level='info': captured.append(message))
        self.assertEqual(captured, [])

    def test_no_notice_for_a_brand_new_user(self):
        import pikpakget.cli as cli
        captured = []
        original = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        self.addCleanup(os.chdir, original)
        cli._warn_about_legacy_state(argparse.Namespace(state_dir=tempfile.mkdtemp()),
                                    lambda message, level='info': captured.append(message))
        self.assertEqual(captured, [])


class TestPreflightSurvivesItsOwnSubject(unittest.TestCase):
    """The first real invocation of `--doctor` on a fresh machine was `--dest
    /volume1/...` on a box with no such path, and the preflight died in a traceback
    instead of reporting the thing it exists to report."""

    def setUp(self):
        import pikpakget.cli as cli
        self.cli = cli
        self.dir = tempfile.mkdtemp()
        blocker = os.path.join(self.dir, 'afile')
        with open(blocker, 'wb'):
            pass
        self.unusable = os.path.join(blocker, 'sub')       # cannot exist: parent is a file

    def test_doctor_reports_an_unusable_state_directory_instead_of_crashing(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = self.cli.main(['--doctor', '--dest', self.unusable,
                                  '--state-dir', self.unusable])
        self.assertEqual(code, 1)
        self.assertIn('状态目录不可用', buffer.getvalue())

    def test_an_ordinary_run_refuses_with_an_exit_code_not_a_traceback(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = self.cli.main(['--status', '--state-dir', self.unusable])
        self.assertEqual(code, 2)
        self.assertIn('状态目录不可用', buffer.getvalue())

    def test_doctor_creates_a_directory_a_run_would_have_created(self):
        import contextlib
        import io
        from pikpakget.pipeline import Log, Pipeline
        import argparse
        dest = os.path.join(self.dir, 'new', 'library')
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'), dest=dest,
                                  max_files=0, connections=1, gap=0, repeat=0, dry_run=False,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=True, no_sweep=True, doctor=False)
        pipeline = Pipeline(args, Log(quiet=True))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pipeline.doctor()
        self.assertTrue(os.path.isdir(dest), buffer.getvalue())
        self.assertIn('已创建', buffer.getvalue())

    def test_run_refuses_an_unbuildable_destination(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state2'),
                                  dest=self.unusable, max_files=0, connections=1, gap=0,
                                  repeat=0, dry_run=False, inventory_only=False, limit=0,
                                  purge_trash=False, no_delete=True, no_sweep=True)
        self.assertEqual(Pipeline(args, Log(quiet=True)).run([]), 2)


class TestDoctor(RunLinkHarness):
    """`--doctor` is what a user runs on a machine nobody has tested on, so its own
    verdicts have to be right: an absent curl with --connections 4 is a failure, a
    stranger's cloud copy is only a warning."""

    def stub_client(self, **overrides):
        import types
        session = types.SimpleNamespace(access_token='a' * 40, user_id='USER',
                                        data={'refresh_token': 'r'},
                                        expires_in=lambda: 3600.0)
        client = types.SimpleNamespace(
            session=session, space=lambda: {'limit': 6442450944, 'usage': 0, 'in_trash': 0,
                                            'free': 6442450944},
            offline_task_slots=lambda: {'limit': 3, 'usage': 0},
            list_folder=lambda parent='*': [], log=lambda *a: None)
        for name, value in overrides.items():
            setattr(client, name, value)
        self.pipeline.client = client
        return client

    def run_doctor(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.pipeline.doctor()
        return code, buffer.getvalue()

    def test_a_healthy_machine_passes(self):
        self.stub_client()
        code, output = self.run_doctor()
        self.assertEqual(code, 0, output)
        self.assertNotIn('FAIL', output)

    def test_no_session_is_a_failure_not_a_note(self):
        import types
        self.stub_client()
        self.pipeline.client.session = types.SimpleNamespace(
            access_token=None, user_id=None, data={}, expires_in=lambda: 0,
            path='/tmp/nowhere/session.json')
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('--login', output)
        self.assertIn('session.json', output, 'the message has to name the file it looked in')

    def test_a_missing_curl_blocks_segmentation(self):
        import types
        self.stub_client()
        real_shutil = self.module.shutil
        self.addCleanup(setattr, self.module, 'shutil', real_shutil)
        self.module.shutil = types.SimpleNamespace(which=lambda name: None,
                                                   disk_usage=real_shutil.disk_usage)
        self.pipeline.args.connections = 4
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('curl', output)

    def test_the_proxy_line_says_what_a_desktop_proxy_would_not(self):
        import unittest.mock
        self.stub_client()
        environment = dict(os.environ)
        environment.pop('HTTPS_PROXY', None)
        environment.pop('https_proxy', None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            _, output = self.run_doctor()
        self.assertIn('不读系统/桌面代理', output)
        with unittest.mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:7890'}):
            _, output = self.run_doctor()
        self.assertIn('127.0.0.1:7890', output)

    def test_a_stranger_on_the_drive_is_only_a_warning(self):
        self.stub_client(list_folder=lambda parent='*': [
            {'kind': 'drive#file', 'id': 'SOMEONE_ELSE', 'size': '1600000000'}])
        code, output = self.run_doctor()
        self.assertEqual(code, 0, output)
        self.assertIn('不是本工具记录的', output)

    def test_a_dead_api_is_a_failure_with_its_reason(self):
        from pikpakget.api import PikPakError
        def refuse():
            raise PikPakError('会话续期被拒', status=401)
        self.stub_client(space=refuse)
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('API 走不通', output)


class TestVolumeFolding(unittest.TestCase):
    """macOS's default APFS and most SMB/CIFS mounts (how a NAS share usually
    reaches a desktop) treat `Movie.mp4` and `movie.mp4` as the *same* path, so
    comparing names exactly hands both files one destination and the second
    download silently replaces the first."""

    def setUp(self):
        import argparse
        from pikpakget.pipeline import Log, Pipeline
        self.dir = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(self.dir, '.state'), dest=self.dir,
                                  max_files=0, connections=1, gap=0, repeat=0, dry_run=False,
                                  inventory_only=False, limit=0, purge_trash=False,
                                  no_delete=True, no_sweep=True)
        self.pipeline = Pipeline(args, Log(quiet=True))
        self.dest = os.path.join(self.dir, 'Series')
        os.makedirs(self.dest)

    def paths(self, folded, *names):
        self.pipeline._case_folded = folded
        self.pipeline.used_names = {}
        out = []
        for index, name in enumerate(names):
            node = {'id': f'F{index}', 'name': name, 'size': 10, 'path': name,
                    'hash': None, 'is_folder': False}
            out.append(os.path.basename(self.pipeline.dest_path(node, self.dest)))
        return out

    def test_a_folded_volume_gives_case_variants_distinct_names(self):
        chosen = self.paths(True, 'Movie.mp4', 'movie.mp4', 'MOVIE.MP4')
        self.assertEqual(len({name.casefold() for name in chosen}), 3, chosen)

    def test_a_strict_volume_needs_no_renaming(self):
        self.assertEqual(self.paths(False, 'Movie.mp4', 'movie.mp4'),
                         ['Movie.mp4', 'movie.mp4'])

    def test_the_probe_agrees_with_the_filesystem(self):
        with open(os.path.join(self.dir, 'CaseProbe.tmp'), 'wb'):
            pass
        real = os.path.exists(os.path.join(self.dir, 'caseprobe.tmp'))
        os.remove(os.path.join(self.dir, 'CaseProbe.tmp'))
        self.pipeline._case_folded = None
        self.assertEqual(self.pipeline.case_insensitive(), real)

    def test_the_probe_cleans_up_after_itself(self):
        self.pipeline.case_insensitive()
        self.assertFalse([name for name in os.listdir(self.dir) if 'case-probe' in name])


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

    def test_a_segment_longer_than_its_range_is_unusable(self):
        # two writers on one file leave a shifted middle: the head is right, and the
        # length can still come out right, so keeping the prefix yields a file that
        # is complete, the correct size and wrong inside
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1500)
        self.assertEqual(self.stream.wipe_overshot([self.item]), [0])
        self.assertFalse(os.path.exists(self.item['path']))
        self.assertEqual(self.stream._segment_have(self.item), 0)

    def test_exact_segment_is_kept(self):
        with open(self.item['path'], 'wb') as handle:
            handle.truncate(1000)
        self.assertEqual(self.stream.wipe_overshot([self.item]), [])
        self.assertTrue(os.path.exists(self.item['path']))
        self.assertEqual(self.stream._segment_have(self.item), 1000)

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

    def test_a_segment_written_past_its_range_is_refetched_whole(self):
        # the byte-count check cannot see this damage, and trusting the prefix is how
        # a 400 MB file ended up right in the head, shifted in the middle, and short
        # by exactly the overlap at the end
        good = self.item(0, 0, 499, have=500)
        bad = self.item(1, 500, 999, have=560)
        self.assertEqual(self.stream.wipe_overshot([good, bad]), [1])
        self.assertFalse(os.path.exists(bad['path']))
        self.assertTrue(os.path.exists(good['path']))
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


class TestSignInDiagnostics(unittest.TestCase):
    """A refusal the user cannot read is a refusal they cannot report — and the body
    that explains it sits next to a live captcha token, which is precisely what would
    end up pasted into an issue."""

    def setUp(self):
        from pikpakget.api import Client
        dirpath = tempfile.mkdtemp()
        self.client = Client(session_path=os.path.join(dirpath, 'session.json'),
                             device_id_path=os.path.join(dirpath, 'device_id'))

    def answer(self, captcha, signin):
        def raw(method, url, **kwargs):
            return captcha if 'captcha' in url else signin
        self.client._raw = raw

    def test_the_refusal_quotes_the_server_but_not_the_token(self):
        from pikpakget.api import PikPakError
        self.answer((200, {'captcha_token': 'LIVE-TOKEN-VALUE'}),
                    (400, {'error_description': 'AccessProhibited', 'tip': 'x' * 400}))
        with self.assertRaises(PikPakError) as caught:
            self.client.sign_in('user@example.com', 'pw')
        text = str(caught.exception)
        self.assertIn('AccessProhibited', text, 'the reason has to survive')
        self.assertNotIn('LIVE-TOKEN-VALUE', text, 'the captcha token must not')
        self.assertLess(len(text), 900, 'long server fields must be clamped')
        self.assertIn('出口地址', text, 'the observed cause belongs in the advice')

    def test_a_captcha_that_never_arrives_is_named_as_such(self):
        from pikpakget.api import PikPakError
        self.answer((400, {'error_description': 'shield blocked'}), (200, {}))
        with self.assertRaises(PikPakError) as caught:
            self.client.sign_in('user@example.com', 'pw')
        self.assertIn('无法取得登录验证码', str(caught.exception))
        self.assertIn('shield blocked', str(caught.exception))


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


    def test_the_last_attempt_keeps_the_bytes_and_marks_them_unverified(self):
        # an unseen block size is not proof of damage, and 400 MB is not free to
        # throw away on a guess: once the retries are spent the bytes stay
        self.assertEqual(self.run_one(size=100, digest='0' * 40)[0], 'retry')
        self.pipeline.state.file('SHAREFILE1')['attempts'] = self.module.MAX_ATTEMPTS
        self.assertEqual(self.run_one(size=100, digest='0' * 40)[0], 'incomplete')
        record = self.pipeline.state.file('SHAREFILE1')
        self.assertEqual(record['state'], 'unverified')
        self.assertTrue(os.path.exists(record['local']), 'the bytes were kept')
        # promote works on `<name>.mp4.part`; naming the quarantine after *that* path
        # left the library with a permanent `.part` that fetch_to read as progress
        self.assertEqual(os.path.basename(record['local']), 'a.mp4.unverified')
        self.assertEqual(os.path.basename(record['home']), 'a.mp4')
        self.assertEqual(record['quarantined'], record['local'])

    def test_a_quarantined_file_is_not_fetched_again_on_the_next_pass(self):
        self.run_one(size=100, digest='0' * 40)
        self.pipeline.state.file('SHAREFILE1')['attempts'] = self.module.MAX_ATTEMPTS
        self.run_one(size=100, digest='0' * 40)
        self.downloads.clear()
        self.assertEqual(self.run_one(size=100, digest='0' * 40)[0], 'incomplete')
        self.assertEqual(self.downloads, [], 'no re-litigating an unproven rule per pass')

    def test_deleting_the_quarantine_asks_for_another_download(self):
        self.run_one(size=100, digest='0' * 40)
        record = self.pipeline.state.file('SHAREFILE1')
        record['attempts'] = self.module.MAX_ATTEMPTS
        self.run_one(size=100, digest='0' * 40)
        os.remove(record['local'])
        self.downloads.clear()
        self.run_one(size=100, digest='0' * 40)
        self.assertEqual(len(self.downloads), 1)

    def test_a_remembered_block_size_is_tried_first(self):
        self.pipeline.hash_hint = None
        sizes = self.pipeline.hash_sizes({'hash_piece': 2 << 20})
        self.assertEqual(sizes[0], 2 << 20)
        self.assertEqual(len(sizes), len(self.module.PIECE_SIZES))
        self.assertEqual(self.pipeline.hash_sizes(None)[0], self.module.PIECE_SIZES[0])
        # the previous file's answer carries over to the next one in the same run
        self.pipeline.hash_hint = 4 << 20
        self.assertEqual(self.pipeline.hash_sizes(None)[0], 4 << 20)


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

    def test_the_auth_endpoint_being_down_is_not_a_dead_session(self):
        # 5xx is the server having a bad minute; treating it as a refusal would stop a
        # multi-day run and send the user to re-login for nothing
        from pikpakget.api import PikPakError
        self.answer((503, {'error_description': 'temporarily unavailable'}))
        with self.assertRaises(PikPakError):
            self.client.refresh()
        self.assertFalse(self.client.session_dead)


class TestUnsupportedPlatform(unittest.TestCase):
    """`import fcntl` at module scope made Windows fail with a traceback before it
    could even print an explanation."""

    def test_windows_refuses_with_advice_and_still_answers_version(self):
        import contextlib
        import io
        import pikpakget.cli as cli
        original = cli.fcntl
        cli.fcntl = None
        self.addCleanup(setattr, cli, 'fcntl', original)
        with contextlib.redirect_stderr(io.StringIO()) as refused:
            self.assertEqual(cli.main(['--whoami']), 2)
        self.assertIn('WSL', refused.getvalue())
        self.assertEqual(cli.main(['--version']), 0)


class TestDailyTrafficCap(unittest.TestCase):
    """A free account gets 20 GB of downstream traffic a day, and the wall it returns
    is a plain HTTP 400 with an upsell body. Nothing in the retry ladder reads that as
    a stop condition, so every remaining file in a 238-file listing would spend its
    attempts against the same limit before the run noticed."""

    CAP_BODY = {'error_description': '{"title":"Today\'s downstream traffic 20.1 G '
                                      'has exceeded the limit 20 G"}', 'error_code': 3}

    def setUp(self):
        from pikpakget.api import Client
        dirpath = tempfile.mkdtemp()
        self.client = Client(session_path=os.path.join(dirpath, 'session.json'),
                             device_id_path=os.path.join(dirpath, 'device_id'))
        self.client.session.store({'access_token': 'a' * 40, 'refresh_token': 'r',
                                   'sub': 'u', 'expires_at': time.time() + 3600})
        self.client.captcha_token = lambda action: 'captcha-token'
        self.slept = []
        import pikpakget.api as api
        original = api.time.sleep
        api.time.sleep = lambda seconds: self.slept.append(seconds)
        self.addCleanup(setattr, api.time, 'sleep', original)

    def feed(self, result):
        self.client._raw = lambda *args, **kwargs: result

    def test_the_cap_is_named_by_type_not_left_as_a_generic_400(self):
        from pikpakget.api import TrafficCapped
        self.feed((400, self.CAP_BODY))
        with self.assertRaises(TrafficCapped) as caught:
            self.client.download_url('FILEID')
        self.assertIn('20.1 G', str(caught.exception), 'the server\'s own figure should survive')
        self.assertEqual(self.slept, [], 'a daily limit must not enter the back-off ladder')

    def test_an_ordinary_400_is_still_just_a_failed_call(self):
        from pikpakget.api import PikPakError, TrafficCapped
        self.feed((400, {'error_description': 'share not found', 'error_code': 40001}))
        with self.assertRaises(PikPakError) as caught:
            self.client.download_url('FILEID')
        self.assertNotIsInstance(caught.exception, TrafficCapped)

    def test_the_first_cap_ends_the_run_without_counting_towards_throttle_rules(self):
        from pikpakget.api import TrafficCapped
        from pikpakget.pipeline import Log, Pipeline
        import argparse
        dirpath = tempfile.mkdtemp()
        args = argparse.Namespace(state_dir=os.path.join(dirpath, '.s'), dest=dirpath,
                                  max_files=0, connections=1, gap=0, repeat=0)
        pipeline = Pipeline(args, Log(quiet=True))
        error = TrafficCapped('今日下行流量已到上限', status=400, code=3)
        self.assertTrue(pipeline.note_throttle(error))
        self.assertEqual(pipeline.throttle_hits, 0, 'it is not a rate limit')
        self.assertEqual(pipeline.auth_hits, 0, 'and not a credential refusal')


class TestCapDoesNotCondemnTheFile(RunLinkHarness):
    """The attempt spent on a request that failed because of the account's daily
    traffic has to come back, or three cap hits mark the file failed for good."""

    def test_a_capped_request_hands_the_attempt_back(self):
        from pikpakget.api import TrafficCapped
        self.pipeline.client.download_url = lambda fid: (_ for _ in ()).throw(
            TrafficCapped('今日下行流量已到上限', status=400, code=3))
        outcome, _ = self.run_one(size=100)
        self.assertEqual(outcome, 'throttled')
        record = self.pipeline.state.data['files']['SHAREFILE1']
        self.assertEqual((record['state'], record['attempts']), ('pending', 0))
        self.assertIn('流量', record['error'])


def flatten(suite):
    """Every leaf test case, however deeply `discover` nested the suites."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class TestDocumentedCounts(unittest.TestCase):
    """Both READMEs quote the number of tests, and both went stale silently. Counting
    the suite from inside it keeps the claim tied to the code.

    The count is deliberately *not* the current run's size: a loader inherited from
    the command line reports 1 test when the suite is filtered with `-k`, which fails
    the very assertion it exists to protect."""

    def test_both_readmes_quote_the_real_number_of_tests(self):
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        loaded = unittest.TestLoader().discover(here, pattern='test_*.py')
        counts = collections.Counter()
        for case in flatten(loaded):
            counts[case.id().split('.')[0]] += 1
        self.assertEqual(sum(counts.values()), loaded.countTestCases())
        claims = {'test_pure': {}, 'test_transfer': {}}
        for name, key, pattern in (
                ('README.md', 'test_pure', r'(\d+) on the pure logic'),
                ('README.cn.md', 'test_pure', r'(\d+) 项纯逻辑测试'),
                ('README.md', 'test_transfer', r'(\d+) real transfers over local HTTP'),
                ('README.cn.md', 'test_transfer', r'(\d+) 项真下载测试')):
            with open(os.path.join(here, '..', name), encoding='utf-8') as handle:
                quoted = re.search(pattern, handle.read())
            self.assertIsNotNone(quoted, f'{name} 里找不到"{key}"的测试数量说法')
            self.assertEqual(int(quoted.group(1)), counts[key],
                             f'{name} 说 {quoted.group(1)} 项 {key}，实际 {counts[key]} 项')
            claims[key][name] = int(quoted.group(1))


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
