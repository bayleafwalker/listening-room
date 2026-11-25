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
import time
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
        scene_shedding.flush()
        if scene_shedding._held is not None:
            os.close(scene_shedding._held)
        patch = mock.patch.object(scene_shedding, '_held', None)
        patch.start()
        self.addCleanup(patch.stop)
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

    def plant(self, number, record):
        directory = self.runtime / 'listening-hub'
        directory.mkdir(exist_ok=True)
        (directory / (scene_shedding.FILE % number)).write_text(record if isinstance(record, str) else json.dumps(record))

    def test_a_stale_file_whose_pid_now_belongs_to_a_live_process_is_not_a_window(self):
        sleeper = subprocess.Popen(['sleep', '30'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        self.plant(sleeper.pid, {'state': 'active', 'gtk': '4.24.0', 'shed': 9, 'resumed': 9, 'pid': sleeper.pid})
        self.assertEqual(status_now()['state'], 'unreported')
        lock = self.runtime / 'listening-hub' / (scene_shedding.LOCK % sleeper.pid)
        lock.write_text('')           # a lock file nobody holds is still not a window
        self.assertEqual(status_now()['state'], 'unreported')

    def test_a_window_in_another_process_is_seen_while_it_runs_and_not_after(self):
        code = ('import os, sys, time; sys.path.insert(0, %r); import scene_shedding as s; '
                's.decided("active", "4.24.0"); s.shed(); s.flush(); print("up", flush=True); time.sleep(60)'
                % str(Path(scene_shedding.__file__).parent))
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), 'up')
        seen = scene_shedding.status()
        self.assertEqual((seen['state'], seen['shed'], seen['windows']), ('active', 1, 1))
        child.kill()
        child.wait()
        self.assertEqual(scene_shedding.status()['state'], 'unreported')

    def test_a_reader_holding_the_lock_file_at_first_report_only_delays_it(self):
        import fcntl
        directory = self.runtime / 'listening-hub'
        directory.mkdir()
        reader = os.open(directory / (scene_shedding.LOCK % os.getpid()), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.path.exists('/proc/self/fd/%d' % reader) and os.close(reader))
        fcntl.flock(reader, fcntl.LOCK_SH)
        scene_shedding.decided('active', '4.24.0')
        scene_shedding.shed()
        time.sleep(.5)                       # still held: nothing reported, nothing lost, window not blocked
        self.assertEqual(scene_shedding.status()['state'], 'unreported')
        os.close(reader)                     # the reader lets go; no further shed or resume happens
        deadline = time.monotonic() + 5
        while scene_shedding.status()['state'] == 'unreported' and time.monotonic() < deadline:
            time.sleep(.05)
        status = scene_shedding.status()
        self.assertEqual((status['state'], status['shed']), ('active', 1))

    def test_hostile_or_damaged_files_are_ignored_and_never_break_status(self):
        for number, record in ((1, '{not json'), (2, {'state': 'bogus', 'pid': 2}), (3, {'pid': 3}),
                               (4, [1, 2]), (5, 'null'), (6, {'state': ['active'], 'pid': 6}),
                               (10 ** 8, {'state': 'active', 'pid': 10 ** 30}), (7, {'state': 'active', 'pid': 10 ** 30}),
                               (0, {'state': 'active', 'pid': 0}), (8, {'state': 'active', 'pid': True}),
                               (1, {'state': 'active', 'pid': 1}), (9, {'state': 'active', 'pid': -9}),
                               (11, {'state': 'active', 'pid': '11'}), (12, {'state': 'active', 'pid': 13})):
            self.plant(number, record)
        directory = self.runtime / 'listening-hub'
        (directory / 'scene-loops-abc.json').write_text('{}')
        (directory / (scene_shedding.FILE % 99)).mkdir()
        status = status_now()
        self.assertEqual((status['state'], status['windows']), ('unreported', 0))
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['listening-roomctl', 'inspect']), \
                mock.patch.object(roomctl.session_client, 'request', return_value={}), mock.patch('sys.stdout', out):
            self.assertEqual(roomctl.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['scene_loop_shedding']['state'], 'unreported')

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
