"""Stopping segmented transfers must preserve pieces without launching more work."""
import os
import tempfile
import types
import unittest
from unittest import mock

from pikpakget import stream
from pikpakget.api import PikPakError


class TestSegmentInterrupt(unittest.TestCase):
    def test_stop_during_backoff_does_not_launch_another_round(self):
        stopped = [False]
        waits = []

        def wait(seconds):
            waits.append(seconds)
            stopped[0] = True

        def spawn(item, url):
            with open(item['path'], 'wb') as handle:
                handle.write(b'ab')
            return types.SimpleNamespace(poll=lambda: 0, wait=lambda **kw: 0)

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(stream, '_spawn', side_effect=spawn) as spawned, \
                mock.patch.object(stream, 'probe_status', return_value='206'):
            # First round is unproductive, leaving an existing prefix unchanged.
            target = os.path.join(root, 'file.part')
            plan = stream.plan_segments(8, 1, target + '.segs1')
            with open(plan[0]['path'], 'wb') as handle:
                handle.write(b'ab')
            with self.assertRaisesRegex(PikPakError, '已中断'):
                stream.download_segments(lambda: 'unused', target, 8, 1,
                                         stop=lambda: stopped[0], wait=wait)
            self.assertEqual(spawned.call_count, 1)
            self.assertEqual(waits, [0.2])
            with open(plan[0]['path'], 'rb') as handle:
                self.assertEqual(handle.read(), b'ab')
            self.assertFalse(os.path.exists(target))

    def test_stop_during_stagger_reaps_started_lane_without_launching_next(self):
        stopped = [False]
        process = types.SimpleNamespace(poll=lambda: 0, wait=mock.Mock())
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(stream, '_spawn', return_value=process) as spawned:
            with self.assertRaisesRegex(PikPakError, '已中断'):
                stream.download_segments(lambda: 'unused', os.path.join(root, 'file'), 8, 2,
                                         wait=lambda seconds: stopped.__setitem__(0, True),
                                         stop=lambda: stopped[0])
            self.assertEqual(spawned.call_count, 1)
            process.wait.assert_called_once_with(timeout=15)

    def test_stop_while_resolving_url_does_not_spawn_curl(self):
        stopped = [False]

        def url():
            stopped[0] = True
            return 'unused'

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(stream, '_spawn') as spawned:
            with self.assertRaisesRegex(PikPakError, '已中断'):
                stream.download_segments(url, os.path.join(root, 'file'), 8, 1,
                                         stop=lambda: stopped[0])
            spawned.assert_not_called()
