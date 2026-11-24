"""`listening-roomctl inspect` reports scene-loop shedding: active, or why not, and how many loops shed and resumed.

The window's process writes a status file (scene_shedding); roomctl reads it. The window hooks themselves
are exercised against real GTK by the o11m probe (the shedding itself) and by a dev-loop run of each state."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_tree  # noqa: E402
source_tree.use_source(Path(__file__).resolve().parents[1] / 'src/listening_hub.py')
import roomctl  # noqa: E402
import scene_shedding  # noqa: E402

KEYS = {'state', 'summary', 'gtk', 'refs_observed', 'sink_finalized', 'shed', 'resumed', 'windows'}


def status_now():
    scene_shedding.flush()
    return scene_shedding.status()


class SceneSheddingStatus(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        patch = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory.name})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(scene_shedding._status, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        scene_shedding._status.update(state=None, gtk=None, refs_observed=None, sink_finalized=None, shed=0, resumed=0)
        patch = mock.patch('atexit.register')
        patch.start()
        self.addCleanup(patch.stop)

    def test_nothing_reported_is_said_plainly_with_every_key(self):
        status = status_now()
        self.assertEqual(set(status), KEYS)
        self.assertEqual((status['state'], status['shed'], status['resumed'], status['windows']), ('unreported', None, None, 0))
        self.assertIn('No open window', status['summary'])

    def test_active_counts_sheds_and_resumes(self):
        scene_shedding.decided('active', '4.24.0')
        for _ in range(3):
            scene_shedding.shed()
        scene_shedding.resumed()
        status = status_now()
        self.assertEqual((status['state'], status['gtk'], status['shed'], status['resumed'], status['refs_observed']),
                         ('active', '4.24.0', 3, 1, None))
        self.assertIn('Shedding is active', status['summary'])
        self.assertIn('3 shed and 1 resumed', status['summary'])

    def test_other_gtk_version_and_missing_libgstgl_and_unreachable_player(self):
        scene_shedding.decided('gtk', '4.20.1')
        status = status_now()
        self.assertEqual((status['state'], status['gtk']), ('gtk', '4.20.1'))
        self.assertIn('GTK 4.20.1', status['summary'])
        scene_shedding.decided('no_libgstgl', '4.24.0')
        self.assertIn('libgstgl', status_now()['summary'])
        scene_shedding.decided('no_player')
        self.assertEqual(status_now()['state'], 'no_player')

    def test_a_refused_release_reports_the_observed_reference_count_and_keeps_counting(self):
        scene_shedding.decided('active', '4.24.0')
        scene_shedding.shed()
        scene_shedding.refused(3, True)
        scene_shedding.resumed()
        status = status_now()
        self.assertEqual((status['state'], status['refs_observed'], status['shed'], status['resumed']), ('refused', 3, 1, 1))
        self.assertIn('3 GL context references where 2 were expected', status['summary'])
        self.assertIn('switched itself off', status['summary'])
        self.assertIs(status['sink_finalized'], True)

    def test_a_refusal_because_the_sink_was_still_held_says_so(self):
        scene_shedding.decided('active', '4.24.0')
        scene_shedding.refused(2, False)
        status = status_now()
        self.assertIs(status['sink_finalized'], False)
        self.assertIn('still held by something else', status['summary'])
        self.assertNotIn('where 2 were expected', status['summary'])

    def test_the_calling_thread_never_touches_a_file(self):
        # o6 criterion 1: no blocking file call on the GTK thread; the shed/resume hooks run there.
        threads = []
        real = scene_shedding._save
        with mock.patch.object(scene_shedding, '_save', side_effect=lambda snap: (threads.append(threading.current_thread()), real(snap))):
            scene_shedding.decided('active', '4.24.0')
            scene_shedding.shed()
            scene_shedding.resumed()
            scene_shedding.flush()
        self.assertTrue(threads)
        self.assertNotIn(threading.current_thread(), threads)
        self.assertEqual(status_now()['shed'], 1)

    def test_a_window_that_is_gone_is_not_reported(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        directory = self.runtime / 'listening-hub'
        directory.mkdir()
        (directory / (scene_shedding.FILE % gone.pid)).write_text(json.dumps(
            {'state': 'active', 'gtk': '4.24.0', 'refs_observed': None, 'shed': 9, 'resumed': 9, 'pid': gone.pid}))
        self.assertEqual(status_now()['state'], 'unreported')

    def test_damaged_or_unknown_files_are_ignored(self):
        directory = self.runtime / 'listening-hub'
        directory.mkdir()
        (directory / (scene_shedding.FILE % 1)).write_text('{not json')
        (directory / (scene_shedding.FILE % 2)).write_text(json.dumps({'state': 'bogus', 'pid': os.getpid()}))
        (directory / (scene_shedding.FILE % 3)).write_text(json.dumps({'pid': os.getpid()}))
        self.assertEqual(status_now()['state'], 'unreported')

    def test_without_a_runtime_directory_nothing_is_written_and_status_is_unreported(self):
        with mock.patch.dict(os.environ):
            del os.environ['XDG_RUNTIME_DIR']
            scene_shedding.decided('active', '4.24.0')
            scene_shedding.shed()
            self.assertEqual(status_now()['state'], 'unreported')
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_roomctl_inspect_adds_the_report_and_keeps_the_controller_snapshot(self):
        scene_shedding.decided('active', '4.24.0')
        scene_shedding.shed()
        scene_shedding.flush()
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['listening-roomctl', 'inspect']), \
                mock.patch.object(roomctl.session_client, 'request', return_value={'version': 2, 'active': None}) as request, \
                mock.patch('sys.stdout', out):
            self.assertEqual(roomctl.main(), 0)
        request.assert_called_once_with('inspect', {})
        printed = json.loads(out.getvalue())
        self.assertEqual((printed['version'], printed['active']), (2, None))
        self.assertEqual(printed['scene_loop_shedding']['state'], 'active')
        self.assertEqual(printed['scene_loop_shedding']['shed'], 1)


if __name__ == '__main__':
    unittest.main()
