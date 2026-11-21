"""Independent acceptance oracles for O10-HYGIENE (wave-2 loose ends).

Owned by the acceptance author, not the implementer. They run in the default suite run; the GUI part (an unset
Familiarity) is the acceptance probe tests/probes/listening-o10h-acceptance.py. Everything here is behaviour:

* criterion 1: the shipped scene tuning loses exactly the 17 unread ``color`` keys and renders byte-identical frames;
* criterion 2 (model part): the label of an unset Familiarity;
* criterion 3: acceptance_verdict.py is run as a program on synthetic runs;
* criterion 4: tests/run_suites.py is run on a copy of tests/ with a decoy suite that starts child processes;
* criterion 5: tests/test_listening_station_eof.py is run against fake players that reproduce the race and that must
  still be failed when the decoder really misbehaves (so a pass cannot come from retrying everything or sleeping);
* criterion 6: scripts/listening-dev.sh is run with fake nix/xvfb-run/dbus-run-session, and the priority of every
  process of the GUI tree is read from /proc.

Where a check reads source text instead of running something, the test says why.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(TESTS))

try:
    import numpy as np  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError:  # pragma: no cover
    np = None


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith('LISTENING_')}
    env.update(PYTHONDONTWRITEBYTECODE='1', **extra)
    return env


# ======================================================================================================================
# Criterion 1: the 17 unread tuning keys are removed and nothing rendered changes
# ======================================================================================================================
# (room, effect index, kind): the 17 shipped `color` keys no effect reads, as recorded by the o9 scenes oracle at e45d947.
UNREAD_COLOR_KEYS = [('after-hours', 2, 'water')] + [
    (room, index, 'electrical') for room, indices in (('after-hours', (5, 6, 7, 8, 9)), ('atomic-lounge', (0, 1, 2, 3, 4, 5)),
                                                      ('midnight-library', (5, 6)), ('velvet-circuit', (0, 1, 2)))
    for index in indices]
MAIN_TUNING = TESTS / 'fixtures' / 'o10h-main-scene-tuning'     # the shipped tuning at e45d947, copied verbatim


def tuning(directory, room):
    return json.loads((Path(directory) / (room + '.json')).read_text())


class TuningKeysGone(unittest.TestCase):
    def test_there_are_seventeen_keys_and_the_fixture_is_the_main_tuning(self):
        # Control for this oracle: the fixture holds exactly the 17 keys on every effect named above.
        self.assertEqual(len(UNREAD_COLOR_KEYS), 17)
        for room, index, kind in UNREAD_COLOR_KEYS:
            effect = tuning(MAIN_TUNING, room)['effects'][index]
            self.assertEqual((effect['kind'], 'color' in effect), (kind, True), (room, index))

    def test_the_seventeen_unread_color_keys_are_removed(self):
        left = []
        for room, index, kind in UNREAD_COLOR_KEYS:
            effect = tuning(SOURCE / 'scene_tuning', room)['effects'][index]
            self.assertEqual(effect['kind'], kind, 'effects are not reordered or removed: %s #%d' % (room, index))
            if 'color' in effect:
                left.append((room, index, kind))
        self.assertEqual(left, [], 'unread color keys still shipped (coordinator decision: remove, do not honour)')

    def test_nothing_else_in_the_tuning_changed(self):
        """The shipped tuning equals the main tuning minus exactly those 17 keys (parsed JSON, so key order and
        whitespace do not matter). Catches 'also retuned something while there', including honouring a colour."""
        expected = {}
        for path in sorted(MAIN_TUNING.glob('*.json')):
            data = json.loads(path.read_text())
            for room, index, _kind in UNREAD_COLOR_KEYS:
                if path.stem == room:
                    del data['effects'][index]['color']
            expected[path.stem] = data
        shipped = {path.stem: json.loads(path.read_text()) for path in sorted((SOURCE / 'scene_tuning').glob('*.json'))}
        self.assertEqual(sorted(shipped), sorted(expected), 'the set of tuning files')
        for room in expected:
            self.assertEqual(shipped[room], expected[room], room)

    def test_no_shipped_tuning_key_is_left_unread(self):
        # Reuses the o9 oracle's run-time recorder (a recording dict in the engine): any shipped key the engine never
        # reads fails, whether it is one of the 17 or a new one. Its KNOWN_UNREAD list is empty after this outcome.
        if np is None:
            self.skipTest('needs Pillow and numpy')
        import test_listening_o9_scenes_oracles as o9
        o9.EngineReads.setUpClass()
        self.assertEqual(o9.EngineReads.KNOWN_UNREAD, set(), 'the o9 oracle must not tolerate any unread key any more')
        self.assertEqual(sorted(o9.EngineReads.unread), [])


@unittest.skipIf(np is None, 'needs Pillow and numpy')
class RenderedScenesUnchanged(unittest.TestCase):
    FRAMES = (0, 97, 240, 401)
    SIZE = 480

    @staticmethod
    def scene_with(identity, directory):
        import visual_scenes
        scene = copy.deepcopy(visual_scenes.SCENES[identity])
        data = tuning(directory, identity.removeprefix('room-'))
        for key in ('motion', 'effects', 'rigid'):
            if key in data:
                scene[key] = data[key]
        return scene

    def frames(self, scene):
        import motion_engine
        with Image.open(SOURCE / 'artwork' / scene['plate']) as plate:
            prepared = motion_engine.prepare_scene(plate, scene)
        out = []
        for frame in self.FRAMES:
            image = motion_engine.room_frame(prepared, frame, self.SIZE)
            image = image if hasattr(image, 'tobytes') else Image.fromarray(image)
            out.append(image.tobytes())
        return out

    def test_every_scene_renders_byte_identical_frames_to_the_main_tuning(self):
        # Compared in one process with the same engine and plate: the main tuning (fixture) against what ships now.
        import visual_scenes
        changed = []
        for identity in sorted(visual_scenes.SCENES):
            room = identity.removeprefix('room-')
            if not (MAIN_TUNING / (room + '.json')).exists():
                continue
            shipped = self.scene_with(identity, SOURCE / 'scene_tuning')
            baseline = self.scene_with(identity, MAIN_TUNING)
            if self.frames(shipped) != self.frames(baseline):
                changed.append(identity)
        self.assertEqual(changed, [], 'rendered frames differ from the main tuning')

    def test_control_the_comparison_sees_a_real_tuning_change(self):
        # Negative control: nudging a read key of an electrical effect must change frames, or the test above is blind.
        identity = 'room-atomic-lounge'
        baseline = self.scene_with(identity, MAIN_TUNING)
        nudged = copy.deepcopy(baseline)
        for effect in nudged['effects']:
            if effect['kind'] == 'electrical':
                effect['strength'] = min(1.0, effect['strength'] * 2 + .2)
        self.assertNotEqual(self.frames(baseline), self.frames(nudged))

    def test_control_the_removed_keys_never_changed_a_frame(self):
        # Precondition the criterion relies on (the keys are unread): main tuning with and without them renders the same.
        for room in ('atomic-lounge', 'midnight-library'):
            identity = 'room-' + room
            with_keys = self.scene_with(identity, MAIN_TUNING)
            without = copy.deepcopy(with_keys)
            for r, index, _kind in UNREAD_COLOR_KEYS:
                if r == room:
                    del without['effects'][index]['color']
            self.assertEqual(self.frames(with_keys), self.frames(without), room)


# ======================================================================================================================
# Criterion 2 (model part): an unset Familiarity is labelled "A mix"
# ======================================================================================================================
class FamiliarityLabel(unittest.TestCase):
    def test_unset_familiarity_is_a_mix_and_every_label_is_a_choice(self):
        import next_station
        for station in ({}, {'preferences': {}}, {'preferences': None}, {'preferences': {'energy': 'Low'}},
                        {'preferences': {'familiarity': None}}):
            chosen = next_station.choices(station)
            self.assertEqual(chosen['familiarity'], 'A mix', station)
            for mood in next_station.MOODS:
                self.assertIn(chosen[mood['key']], [label for label, _value in mood['choices']],
                              'the shown label is one the control offers: %s %r' % (mood['key'], station))

    def test_stored_values_keep_their_labels(self):
        import next_station
        for value, label in (('Familiar', 'Familiar'), ('Any', 'A mix'), ('New', 'Discover')):
            self.assertEqual(next_station.choices({'preferences': {'familiarity': value}})['familiarity'], label)

    def test_other_unset_moods_still_show_any(self):
        import next_station
        chosen = next_station.choices({'preferences': {}})
        self.assertEqual((chosen['energy'], chosen['speech'], chosen['stay_close']), ('Any', 'Any', 'Any'))


# ======================================================================================================================
# Criterion 3: acceptance_verdict.py, run as a program
# ======================================================================================================================
class Verdict(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='o10h-verdict-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'probes' / 'expected').mkdir(parents=True)
        shutil.copy(TESTS / 'probes' / 'acceptance_verdict.py', self.tmp / 'probes')

    def run_verdict(self, expected_lines, results, name='t'):
        (self.tmp / 'probes' / 'expected' / (name + '.checks')).write_text('\n'.join(expected_lines) + '\n')
        out = self.tmp / ('run-' + name)
        out.mkdir(exist_ok=True)
        (out / 'acceptance-x.json').write_text(json.dumps({'results': [{'criterion': c, 'pass': ok} for c, ok in results]}))
        proc = subprocess.run([sys.executable, str(self.tmp / 'probes' / 'acceptance_verdict.py'), name, str(out)],
                              capture_output=True, text=True, timeout=60, env=clean_env())
        return proc.returncode, proc.stdout + proc.stderr

    # -- @@ still does its job -------------------------------------------------------------------------------------
    def test_a_span_matches_the_measurements_probes_really_print(self):
        entry = ['a within 150 ms (measured @@ ms)']
        for measured in ('12', '12.5', '-3.5', '+4', '0.25', '[12.5, 14.0, 9]', '[1.0, 2.5]'):
            rc, out = self.run_verdict(entry, [('a within 150 ms (measured %s ms)' % measured, True)])
            self.assertEqual(rc, 0, '%r: %s' % (measured, out))

    def test_every_span_of_a_name_is_checked_and_the_rest_is_exact(self):
        entry = ['x (gaps @@ ms), never more than @@ ms']
        self.assertEqual(self.run_verdict(entry, [('x (gaps 1.5 ms), never more than 400 ms', True)])[0], 0)
        self.assertNotEqual(self.run_verdict(entry, [('x (gaps 1.5 ms), never more than four ms', True)])[0], 0)
        # the literal text around a span is identity: a changed threshold is a different (missing) check
        self.assertNotEqual(self.run_verdict(['a within 150 ms (measured @@ ms)'],
                                             [('a within 999 ms (measured 12 ms)', True)])[0], 0)

    # -- @@ no longer matches letters (adversarial) ----------------------------------------------------------------------
    def test_a_span_does_not_swallow_words(self):
        entry = ['a within 150 ms (measured @@ ms)']
        for swallowed in ('broken', 'FAILED to settle', 'NaN', 'timed out: 9', 'ok', 'twelve', '12 and then it crashed',
                          '12 ms) and also (measured 3', '12\nFAILED\n13'):
            rc, out = self.run_verdict(entry, [('a within 150 ms (measured %s ms)' % swallowed, True)])
            self.assertNotEqual(rc, 0, 'a span matched %r: %s' % (swallowed, out))
            self.assertIn('a within 150 ms', out, 'the message names the check that no longer matches / went missing')

    def test_a_failing_check_with_a_changed_name_is_not_hidden_by_a_span(self):
        # The realistic fake: a probe that now reports the failure inside the span text must not be read as the
        # listed measurement (which would turn "FAILED" into a pass of the listed check).
        entry = ['b settles (worst @@ ms)']
        rc, out = self.run_verdict(entry, [('b settles (worst gave up ms)', False)])
        self.assertNotEqual(rc, 0, out)

    def test_a_listed_passing_check_that_fails_with_a_valid_span_still_fails(self):
        rc, out = self.run_verdict(['b settles (worst @@ ms)'], [('b settles (worst 12 ms)', False)])
        self.assertNotEqual(rc, 0, out)
        self.assertIn('FAILED', out)

    # -- ! together with @@ is refused (adversarial) -----------------------------------------------------------------------
    def test_a_known_failure_with_a_span_is_refused_with_a_message(self):
        for passing in (False, True):
            for lines in (['!a (worst @@ ms)'], ['ok one', '!a (worst @@ ms)', 'ok two'], ['# why', '!a @@']):
                results = [('ok one', True), ('ok two', True), ('a (worst 5 ms)', passing), ('a 5', passing)]
                rc, out = self.run_verdict(lines, results)
                self.assertNotEqual(rc, 0, 'silently accepted %r (check passes=%s): %s' % (lines, passing, out))
                self.assertIn('@@', out, 'the message names the span marker')
                self.assertIn('!', out, 'the message names the known-failure marker')
                self.assertNotIn('Traceback', out, 'a refusal with a message, not a crash')

    def test_a_known_failure_with_a_span_is_refused_even_when_nothing_else_is_wrong(self):
        rc, out = self.run_verdict(['!a (worst @@ ms)'], [('a (worst 5 ms)', False)])
        self.assertNotEqual(rc, 0, out)

    # -- existing behaviour is kept ----------------------------------------------------------------------------------------
    def test_exact_known_failures_and_missing_checks_work_as_before(self):
        self.assertEqual(self.run_verdict(['ok', '!5i [ORIGINAL] (x)'], [('ok', True), ('5i [ORIGINAL] (x)', False)])[0], 0)
        rc, out = self.run_verdict(['ok', '!old'], [('ok', True), ('old', True)])
        self.assertNotEqual(rc, 0)
        self.assertIn('known failure now passes', out)
        rc, out = self.run_verdict(['ok', 'gone'], [('ok', True)])
        self.assertNotEqual(rc, 0)
        self.assertIn('MISSING', out)
        self.assertNotEqual(self.run_verdict(['ok'], [('ok', True), ('new but failing', False)])[0], 0)
        self.assertEqual(self.run_verdict(['ok'], [('ok', True), ('new and passing', True)])[0], 0)

    def test_every_shipped_expected_file_is_still_accepted(self):
        # Each shipped expected file, replayed as a run that produces exactly its checks (a measurement of 12.5 in every
        # span, the listed known failures failing). The implementer must not make the real files unreadable.
        files = sorted((TESTS / 'probes' / 'expected').glob('*.checks'))
        self.assertTrue(files)
        for path in files:
            results = []
            for line in path.read_text().splitlines():
                if not line or line.startswith('#'):
                    continue
                known = line.startswith('!')
                name = (line[1:] if known else line).replace('@@', '12.5')
                results.append((name, not known))
            (self.tmp / 'probes' / 'expected' / ('replay-' + path.name)).write_text(path.read_text())
            out = self.tmp / ('replay-' + path.stem)
            out.mkdir(exist_ok=True)
            (out / 'acceptance-x.json').write_text(json.dumps({'results': [{'criterion': c, 'pass': ok} for c, ok in results]}))
            proc = subprocess.run([sys.executable, str(self.tmp / 'probes' / 'acceptance_verdict.py'), 'replay-' + path.stem, str(out)],
                                  capture_output=True, text=True, timeout=60, env=clean_env())
            # the verdict names the file 'replay-NAME.checks'; copy under that name
            self.assertEqual(proc.returncode, 0, path.name + ': ' + proc.stdout + proc.stderr)


# ======================================================================================================================
# Criterion 4: netguard follows child processes of a suite
# ======================================================================================================================
DECOY = r'''
import json, os, shutil, socket, subprocess, sys, threading, unittest

CHILD = """
import json, socket, sys
def classify(call):
    try:
        got = call()
    except BaseException as error:
        text = type(error).__name__ + ' ' + str(error)
        return 'blocked' if 'block' in text.lower() else 'other:' + type(error).__name__
    return 'connected' if got in (None, 0) else 'other:' + repr(got)
port = int(sys.argv[1])
unix = sys.argv[2]
def tcp():
    return socket.create_connection(('192.0.2.1', 9), timeout=1).close()
def ex():
    s = socket.socket(); s.settimeout(1)
    try:
        return s.connect_ex(('192.0.2.1', 9))
    finally:
        s.close()
def lookup():
    return socket.getaddrinfo('example.invalid', 80) and None
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return s.sendto(b'x', ('192.0.2.1', 9)) and None
    finally:
        s.close()
def loopback():
    socket.create_connection(('127.0.0.1', port), timeout=2).close()
def unixsock():
    s = socket.socket(socket.AF_UNIX); s.connect(unix); s.close()
only = sys.argv[3] if len(sys.argv) > 3 else ''
out = {name: classify(fn) for name, fn in (('tcp', tcp), ('connect_ex', ex), ('lookup', lookup), ('udp', udp), ('loopback', loopback), ('unix', unixsock))}
if only == 'grandchild':
    import subprocess
    p = subprocess.run([sys.executable, '-c', sys.argv[4], str(port), unix], capture_output=True, text=True)
    out['grandchild'] = json.loads(p.stdout.strip().splitlines()[-1])
print(json.dumps(out))
"""
CHILD_AGAIN = CHILD.replace("only = sys.argv[3] if len(sys.argv) > 3 else ''", "only = ''")


class Children(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socket.socket(); cls.server.bind(('127.0.0.1', 0)); cls.server.listen(16)
        cls.port = cls.server.getsockname()[1]
        threading.Thread(target=cls.accept, daemon=True).start()
        import tempfile
        cls.unix_dir = tempfile.mkdtemp(prefix='nguard-')
        cls.unix_path = os.path.join(cls.unix_dir, 's')
        cls.unix_server = socket.socket(socket.AF_UNIX); cls.unix_server.bind(cls.unix_path); cls.unix_server.listen(4)
        threading.Thread(target=lambda: [cls.unix_server.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()

    @classmethod
    def accept(cls):
        while True:
            try:
                cls.server.accept()[0].close()
            except OSError:
                return

    def run_child(self, argv, **kwargs):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, **kwargs)
        self.assertTrue(proc.stdout.strip(), 'the child produced no result: %r' % proc.stderr[-300:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def expect_guarded(self, result, who):
        self.assertEqual({k: result[k] for k in ('tcp', 'connect_ex', 'lookup', 'udp')},
                         {'tcp': 'blocked', 'connect_ex': 'blocked', 'lookup': 'blocked', 'udp': 'blocked'}, who)
        self.assertEqual((result['loopback'], result['unix']), ('connected', 'connected'), who + ': loopback and unix stay usable')

    def args(self, *extra):
        return [str(self.port), self.unix_path] + list(extra)

    def test_control_the_suite_process_itself_is_guarded(self):
        with self.assertRaises(OSError) as caught:
            socket.create_connection(('192.0.2.1', 9), timeout=1)
        self.assertIn('block', str(caught.exception).lower())

    def test_python_child_by_sys_executable(self):
        self.expect_guarded(self.run_child([sys.executable, '-c', CHILD] + self.args()), 'sys.executable child')

    def test_python3_child_found_on_path(self):
        python3 = shutil.which('python3')
        if not python3:
            self.skipTest('no python3 on PATH')
        self.expect_guarded(self.run_child([python3, '-c', CHILD] + self.args()), 'python3 child')

    def test_child_started_through_a_shell(self):
        quoted = lambda s: "'" + s.replace("'", "'\\''") + "'"
        command = ' '.join([quoted(sys.executable), '-c', quoted(CHILD)] + [quoted(a) for a in self.args()])
        self.expect_guarded(self.run_child(['sh', '-c', command]), 'shell child')

    def test_child_given_a_copy_of_the_environment(self):
        env = dict(os.environ)
        env['SOMETHING_ELSE'] = '1'
        self.expect_guarded(self.run_child([sys.executable, '-c', CHILD] + self.args(), env=env), 'child with a copied environment')

    def test_child_given_a_filtered_copy_of_the_environment(self):
        # What the GUI harness (o6_env) does when it re-executes itself: drop a few variables, keep the rest.
        env = {k: v for k, v in os.environ.items() if k not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE')}
        self.expect_guarded(self.run_child([sys.executable, '-c', CHILD] + self.args(), env=env), 'child with a filtered environment')

    def test_grandchild(self):
        result = self.run_child([sys.executable, '-c', CHILD] + self.args('grandchild', CHILD_AGAIN))
        self.expect_guarded(result, 'child')
        self.expect_guarded(result['grandchild'], 'grandchild')

    def test_child_running_a_script_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'child.py')
            open(path, 'w').write(CHILD)
            self.expect_guarded(self.run_child([sys.executable, path] + self.args()), 'script child')


if __name__ == '__main__':
    unittest.main()
'''


class NetguardChildren(unittest.TestCase):
    """Runs the real tests/run_suites.py on a copy of tests/ that holds one decoy suite, so the guard is judged the way
    a suite sees it. Scenario by scenario, because each failure is a different way to slip past the guard."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='o10h-netguard-'))
        copy = cls.tmp / 'tests'
        shutil.copytree(TESTS, copy, ignore=shutil.ignore_patterns('test_*.py', 'probes', 'fixtures', '__pycache__'))
        (copy / 'suites-manual.txt').write_text('# none\n')
        (copy / 'test_listening_decoy_children.py').write_text(DECOY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def runner_output(cls):
        # One run of the decoy covers all scenarios; unittest -v lines say which one failed.
        if getattr(cls, '_output', None) is None:
            env = clean_env(PYTHONUNBUFFERED='1')
            proc = subprocess.run([sys.executable, str(cls.tmp / 'tests' / 'run_suites.py'), 'test_listening_decoy_children'],
                                  cwd=cls.tmp, env=env, capture_output=True, text=True, timeout=300)
            cls._output = (proc.returncode, proc.stdout + proc.stderr)
        return cls._output

    def scenario(self, test):
        rc, out = self.runner_output()
        lines = [line for line in out.splitlines() if (' ' + test) in line or ('(%s' % test) in line or line.startswith(('FAIL: ' + test, 'ERROR: ' + test))]
        failed = any(('FAIL: ' + test) in line or ('ERROR: ' + test) in line for line in out.splitlines())
        ran = 'Ran ' in out
        self.assertTrue(ran, 'the decoy suite did not run: ' + out[-600:])
        self.assertFalse(failed, 'a child escaped the guard in %s:\n%s' % (test, out[-1800:]))

    def test_control_the_suite_itself_is_guarded(self):
        self.scenario('test_control_the_suite_process_itself_is_guarded')

    def test_python_child_by_sys_executable_is_guarded(self):
        self.scenario('test_python_child_by_sys_executable')

    def test_python3_found_on_path_is_guarded(self):
        self.scenario('test_python3_child_found_on_path')

    def test_child_through_a_shell_is_guarded(self):
        self.scenario('test_child_started_through_a_shell')

    def test_child_with_a_copied_environment_is_guarded(self):
        self.scenario('test_child_given_a_copy_of_the_environment')

    def test_child_with_a_filtered_environment_is_guarded(self):
        self.scenario('test_child_given_a_filtered_copy_of_the_environment')

    def test_grandchild_is_guarded(self):
        self.scenario('test_grandchild')

    def test_script_file_child_is_guarded(self):
        self.scenario('test_child_running_a_script_file')

    def test_the_runner_still_exits_zero_when_every_child_is_refused(self):
        rc, out = self.runner_output()
        self.assertEqual(rc, 0, out[-1800:])

    def test_the_guard_is_not_left_on_in_an_unguarded_python(self):
        # Reason for a negative check: an env-based guard must not leak into processes the suite runner did not start
        # (a plain interpreter in this test must still reach getaddrinfo of loopback names normally).
        proc = subprocess.run([sys.executable, '-c', 'import socket; print(socket.getaddrinfo("localhost", 80)[0][0])'],
                              capture_output=True, text=True, timeout=30, env=clean_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ======================================================================================================================
# Criterion 5: station_eof, judged against fake players
# ======================================================================================================================
FAKE_MPV = r'''#!%(python)s
import json, os, socket, sys, threading, time
mode = os.environ.get('FAKE_MPV_MODE', 'ok')
path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--input-ipc-server='))
started = time.monotonic()
server = socket.socket(socket.AF_UNIX)
server.bind(path)                       # the socket file exists from here on, as with the real player
if mode.startswith('race:'):
    time.sleep(float(mode.split(':')[1]))   # ... but connect() is refused until listen()
if mode != 'never_listen':
    server.listen(8)

def value(name):
    age = time.monotonic() - started
    if name == 'eof-reached':
        if mode == 'never_eof':
            return False
        if age < .25 + (float(mode.split(':')[1]) if mode.startswith('race:') else 0):
            return None                  # property unavailable while the file is still playing
        if mode == 'eof_flips' and age > .25 + .10:
            return False
        if mode == 'exits_at_eof':
            threading.Timer(.05, lambda: os._exit(0)).start()    # the player goes away right after it reports EOF
        return True
    if name == 'idle-active':
        return mode == 'idle_true'
    return None

def serve(connection):
    connection.sendall(b'{"event":"idle"}\n')
    for line in connection.makefile('rb'):
        request = json.loads(line)
        name = request['command'][1]
        data = value(name)
        reply = {'request_id': request.get('request_id'), 'error': 'success', 'data': data} if data is not None \
            else {'request_id': request.get('request_id'), 'error': 'property unavailable'}
        connection.sendall(json.dumps(reply).encode() + b'\n')

if mode == 'dies_early':
    os._exit(3)
if mode != 'never_listen':
    while True:
        connection, _ = server.accept()
        threading.Thread(target=serve, args=(connection,), daemon=True).start()
else:
    time.sleep(60)
'''


class StationEof(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='o10h-eof-'))
        cls.player = cls.tmp / 'mpv'
        cls.player.write_text(FAKE_MPV % {'python': sys.executable})
        cls.player.chmod(cls.player.stat().st_mode | stat.S_IXUSR)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def run_suite(self, mode, timeout=60):
        env = clean_env(LISTENING_TEST_MPV=str(self.player), FAKE_MPV_MODE=mode)
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(TESTS / 'test_listening_station_eof.py'), '-v'], cwd=TESTS, env=env,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start

    def assertPasses(self, mode):
        rc, out, took = self.run_suite(mode)
        self.assertEqual(rc, 0, mode + ': ' + out[-800:])
        self.assertIn('Ran 1 test', out)
        self.assertNotIn('skipped', out, 'passing by skipping is not passing: ' + out[-400:])
        return took

    def assertFails(self, mode):
        rc, out, took = self.run_suite(mode)
        self.assertNotEqual(rc, 0, '%s: the suite passed against a player that misbehaves: %s' % (mode, out[-600:]))
        self.assertNotIn('skipped', out, mode + ': a skip is not a failure to see')
        self.assertLess(took, 40, mode + ': the suite hangs or waits far beyond its own deadline')

    # -- the race ------------------------------------------------------------------------------------------------------
    def test_a_player_that_is_well_behaved_passes_quickly(self):
        took = self.assertPasses('ok')
        self.assertLess(took, 8, 'a prompt player must not cost a long fixed sleep (took %.1f s)' % took)

    def test_the_socket_file_exists_before_the_player_listens_short_gap(self):
        self.assertPasses('race:0.3')

    def test_the_socket_file_exists_before_the_player_listens_long_gap(self):
        # A fixed 0.1-0.5 s sleep does not cover this; waiting for readiness does.
        self.assertPasses('race:1.5')

    def test_race_repeated(self):
        for gap in (0.05, 0.15, 0.6, 1.0):
            self.assertPasses('race:%s' % gap)

    # -- the fix must not make the test pass for a decoder that is wrong ---------------------------------------------------
    def test_a_player_that_never_reaches_eof_fails(self):
        self.assertFails('never_eof')

    def test_a_player_that_never_listens_fails_within_the_deadline(self):
        self.assertFails('never_listen')

    def test_a_player_that_dies_before_it_serves_fails(self):
        self.assertFails('dies_early')

    def test_a_player_that_exits_at_eof_fails(self):
        self.assertFails('exits_at_eof')

    def test_a_player_that_drops_eof_again_fails(self):
        self.assertFails('eof_flips')

    def test_a_player_that_reports_idle_fails(self):
        self.assertFails('idle_true')

    # -- the suite is back in the default run ---------------------------------------------------------------------------------
    def test_the_suite_runs_by_default(self):
        listed = subprocess.run([sys.executable, str(TESTS / 'run_suites.py'), '--list'], capture_output=True, text=True,
                                timeout=60, env=clean_env()).stdout.split()
        self.assertIn('test_listening_station_eof.py', listed)
        manual = [line.split()[0] for line in (TESTS / 'suites-manual.txt').read_text().splitlines()
                  if line.strip() and not line.startswith('#')]
        self.assertNotIn('test_listening_station_eof.py', manual)


# ======================================================================================================================
# Criterion 6: the dev loop runs GUI children at a lower priority
# ======================================================================================================================
def priority_of(pid):
    """(nice, scheduling policy, cpu.weight of its cgroup or None) from /proc, for any process."""
    with open('/proc/%d/stat' % pid) as handle:
        fields = handle.read().rsplit(')', 1)[1].split()
    nice = int(fields[16])
    policy = os.sched_getscheduler(pid)
    weight = None
    try:
        with open('/proc/%d/cgroup' % pid) as handle:
            lines = handle.read().splitlines()
        for line in lines:
            if line.startswith('0::'):
                with open('/sys/fs/cgroup%s/cpu.weight' % line[3:]) as handle:
                    weight = int(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        weight = None
    return {'nice': nice, 'policy': policy, 'weight': weight}


RECORDER = '''#!%(python)s
import json, os, sys
sys.path.insert(0, %(here)r)
from prio_lib import priority_of
label, pid = sys.argv[1], int(sys.argv[2])
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'label': label, 'prio': priority_of(pid)}) + '\\n')
'''

FAKE_PROBE = '''import json, os, subprocess, sys
sys.path.insert(0, %(here)r)
from prio_lib import priority_of
label = os.path.basename(sys.argv[0])
helper = subprocess.Popen(['sleep', '30'])
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'label': label, 'prio': priority_of(os.getpid()), 'argv': sys.argv[1:],
                          'env': {k: os.environ.get(k) for k in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS',
                                  'HYPRLAND_INSTANCE_SIGNATURE', 'GDK_BACKEND', 'GDK_DEBUG', 'XDG_RUNTIME_DIR')}}) + '\\n')
    log.write(json.dumps({'label': label + ' helper child', 'prio': priority_of(helper.pid)}) + '\\n')
helper.kill()
out = sys.argv[sys.argv.index('--output') + 1]
os.makedirs(out, exist_ok=True)
if 'acceptance' in label:
    ok = os.environ.get('FAKE_ACCEPTANCE_PASS', '1') == '1'
    json.dump({'results': [{'criterion': 'one', 'pass': True}, {'criterion': 'two', 'pass': ok}]},
              open(os.path.join(out, 'acceptance-fake.json'), 'w'))
'''

FAKE_NIX = '''#!/bin/sh
case "$1" in
  build) echo "$FAKE_ASSETS" ;;
  develop) shift; shift; if [ "$1" = -c ] || [ "$1" = --command ]; then shift; fi; exec "$@" ;;
  *) echo "fake nix: unsupported $*" >&2; exit 9 ;;
esac
'''

FAKE_XVFB = '''#!/bin/sh
# Like xvfb-run: start a stand-in X server in the background, run the command, stop the server.
while [ $# -gt 0 ]; do case "$1" in -a) shift ;; -s) shift; shift ;; *) break ;; esac; done
sleep 30 &
server=$!
%(python)s %(here)s/record.py xvfb-run $$
%(python)s %(here)s/record.py x-server $server
"$@"
status=$?
kill $server 2>/dev/null
exit $status
'''

FAKE_DBUS = '''#!/bin/sh
%(python)s %(here)s/record.py dbus-run-session $$
exec "$@"
'''


class DevLoopPriority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='o10h-dev-'))
        repo = cls.repo = cls.tmp / 'repo'
        (repo / 'scripts').mkdir(parents=True)
        (repo / 'tests' / 'probes' / 'expected').mkdir(parents=True)
        shutil.copy(ROOT / 'scripts' / 'listening-dev.sh', repo / 'scripts')
        shutil.copy(TESTS / 'probes' / 'acceptance_verdict.py', repo / 'tests' / 'probes')
        here = str(cls.tmp / 'helpers')
        (cls.tmp / 'helpers').mkdir()
        import inspect
        (cls.tmp / 'helpers' / 'prio_lib.py').write_text('import os\n' + inspect.getsource(priority_of))
        (cls.tmp / 'helpers' / 'record.py').write_text(RECORDER % {'python': sys.executable, 'here': here})
        probes = repo / 'tests' / 'probes'
        for name in ('fake-evidence', 'fake-acceptance'):
            (probes / (name + '.py')).write_text(FAKE_PROBE % {'here': here})
        (probes / 'fake-plain.py').write_text(FAKE_PROBE % {'here': here})
        (probes / 'acceptance.manifest').write_text('# fake\nf1 fake-evidence fake-acceptance\nf2 - fake-acceptance\n')
        for name in ('f1', 'f2'):
            (probes / 'expected' / (name + '.checks')).write_text('one\ntwo\n')
        bin_dir = cls.tmp / 'bin'
        bin_dir.mkdir()
        for name, body in (('nix', FAKE_NIX), ('xvfb-run', FAKE_XVFB % {'python': sys.executable, 'here': here}),
                           ('dbus-run-session', FAKE_DBUS % {'python': sys.executable, 'here': here})):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        # python3 for the scripts: the interpreter running this test
        (bin_dir / 'python3').symlink_to(sys.executable)
        cls.bin = bin_dir
        cls.assets = cls.tmp / 'assets'
        cls.assets.mkdir()
        cls.base = priority_of(os.getpid())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def run_script(self, *args, passing='1'):
        log = self.tmp / ('log-%d.jsonl' % time.monotonic_ns())
        env = clean_env(PATH='%s:%s' % (self.bin, os.environ['PATH']), FAKE_LOG=str(log), FAKE_ASSETS=str(self.assets),
                        FAKE_ACCEPTANCE_PASS=passing, WAYLAND_DISPLAY='wayland-live', HYPRLAND_INSTANCE_SIGNATURE='live',
                        DBUS_SESSION_BUS_ADDRESS='unix:path=/run/live-bus', TMPDIR=str(self.tmp))
        proc = subprocess.run(['bash', str(self.repo / 'scripts' / 'listening-dev.sh'), *args], capture_output=True, text=True,
                              timeout=120, env=env, cwd=self.tmp)
        records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return proc, records

    def lower(self, prio):
        """Is this process at a lower CPU priority than a default process started from this test?"""
        base = self.base
        if prio['nice'] > base['nice']:
            return True
        if prio['policy'] != base['policy'] and prio['policy'] in (getattr(os, 'SCHED_IDLE', -1), getattr(os, 'SCHED_BATCH', -1)):
            return True
        return prio['weight'] is not None and base['weight'] is not None and prio['weight'] < base['weight']

    def check_all_lower(self, records, labels):
        seen = {r['label'] for r in records}
        self.assertEqual(sorted(seen), sorted(labels), 'the GUI tree the script starts')
        if self.base['nice'] >= 19 and self.base['weight'] in (None, 1):
            self.skipTest('this test process is already at the lowest priority; run it at nice 0')
        slow = {r['label']: r['prio'] for r in records if not self.lower(r['prio'])}
        self.assertEqual(slow, {}, 'at the same priority as a default process (parent %r)' % self.base)

    # -- every process of the GUI tree --------------------------------------------------------------------------------------
    def test_probe_runs_the_whole_gui_tree_at_lower_priority(self):
        proc, records = self.run_script('probe', str(self.tmp / 'out-probe'), 'fake-plain')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.check_all_lower(records, ['xvfb-run', 'x-server', 'dbus-run-session', 'fake-plain.py', 'fake-plain.py helper child'])

    def test_acceptance_runs_the_evidence_and_the_acceptance_probe_at_lower_priority(self):
        proc, records = self.run_script('acceptance', str(self.tmp / 'out-acc'))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        labels = [r['label'] for r in records]
        # f1: evidence + acceptance probe; f2: acceptance probe only. Each under its own xvfb-run / dbus-run-session.
        self.assertEqual(labels.count('fake-evidence.py'), 1)
        self.assertEqual(labels.count('fake-acceptance.py'), 2)
        self.assertEqual(labels.count('x-server'), 3)
        self.check_all_lower(records, ['xvfb-run', 'x-server', 'dbus-run-session', 'fake-evidence.py', 'fake-evidence.py helper child',
                                       'fake-acceptance.py', 'fake-acceptance.py helper child'])

    # -- nothing else about the run changed ----------------------------------------------------------------------------------
    def test_arguments_and_isolation_are_unchanged(self):
        proc, records = self.run_script('probe', str(self.tmp / 'out-args'), 'fake-plain')
        probe = next(r for r in records if r['label'] == 'fake-plain.py')
        self.assertEqual(probe['argv'], ['--output', str(self.tmp / 'out-args')])
        env = probe['env']
        self.assertIsNone(env['WAYLAND_DISPLAY'])
        self.assertIsNone(env['DBUS_SESSION_BUS_ADDRESS'])
        self.assertIsNone(env['HYPRLAND_INSTANCE_SIGNATURE'])
        self.assertEqual((env['GDK_BACKEND'], env['GDK_DEBUG']), ('x11', 'no-portals'))
        self.assertTrue(env['XDG_RUNTIME_DIR'] and env['XDG_RUNTIME_DIR'].startswith(str(self.tmp)), env)
        self.assertFalse(Path(env['XDG_RUNTIME_DIR']).exists(), 'the private runtime dir is removed afterwards')

    def test_acceptance_arguments_and_verdict_exit_status_are_unchanged(self):
        proc, records = self.run_script('acceptance', str(self.tmp / 'out-a1'), 'f2')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('f2: 2 passed', proc.stdout)
        probe = next(r for r in records if r['label'] == 'fake-acceptance.py')
        self.assertEqual(probe['argv'], ['--repo', str(self.repo), '--output', str(self.tmp / 'out-a1' / 'f2')])
        proc, _ = self.run_script('acceptance', str(self.tmp / 'out-a2'), 'f2', passing='0')
        self.assertNotEqual(proc.returncode, 0, 'a failing probe still fails the run')
        self.assertIn('FAILED two', proc.stdout)

    def test_usage_and_test_subcommand_are_not_disturbed(self):
        proc, _ = self.run_script('bogus')
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
