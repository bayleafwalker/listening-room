"""Independent acceptance oracles for the O5 finish that need no GTK display.

Owned by the acceptance author, not the implementer. They bind the launch contract, the shared home module's
entry points and the room-volume agreement to the repository as it is, and keep the behaviours of the retired
classic tests that still exist (discovery rules, station save/ambience rules) pinned to the code that now serves
them. The parity gap oracles that must FAIL while a contract capability has no path live in
tests/probes/listening-o5-acceptance.py, so that this suite stays green in the package build.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import next_window
except (ImportError, ValueError):   # no GTK typelibs in this environment
    next_window = None
import listening_room
import next_model


class LaunchContract(unittest.TestCase):
    def test_classic_is_refused_with_status_two_and_a_message(self):
        for argv in (['--ui=classic'], ['--ui', 'classic'], ['--desk', '--ui=classic']):
            with self.subTest(argv=argv), mock.patch.object(sys, 'argv', ['listening-room'] + argv):
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    status = listening_room.main()
                self.assertEqual(status, 2)
                self.assertIn('classic', error.getvalue())

    def test_the_default_and_next_open_the_one_window(self):
        if next_window is None:
            self.skipTest('GTK4 bindings unavailable')
        seen = []
        with mock.patch.object(next_window, 'run', side_effect=lambda argv: seen.append(list(argv)) or 0):
            for argv in ([], ['--ui=next'], ['--mini'], ['--desk'], ['--inside'], ['--preview-link', 'https://example.org/a.mp3']):
                with mock.patch.object(sys, 'argv', ['listening-room'] + argv):
                    self.assertEqual(listening_room.main(), 0)
        self.assertEqual(len(seen), 6)

    def test_desk_and_mini_are_the_same_launch(self):
        if next_window is None:
            self.skipTest('GTK4 bindings unavailable')
        self.assertEqual(next_window.launch_arguments(['--desk']), next_window.launch_arguments(['--mini']))
        self.assertEqual(next_window.launch_arguments(['--desk'])[0], 'mini')

    def test_a_link_uri_is_a_link_not_a_mode(self):
        if next_window is None:
            self.skipTest('GTK4 bindings unavailable')
        mode, link = next_window.launch_arguments(['listening-room://open?url=https%3A%2F%2Fexample.org%2Fa.mp3'])
        self.assertEqual((mode, link), ('browse', 'https://example.org/a.mp3'))

    def test_the_removed_modules_are_gone_and_nothing_imports_them(self):
        gone = ('room_experience', 'connections_ui', 'twitch_ui', 'world_radio_ui', 'room_discovery')
        for name in gone:
            self.assertFalse((SOURCE / (name + '.py')).exists(), name)
        for path in list(SOURCE.glob('*.py')) + list(Path(__file__).resolve().parent.glob('test_listening_*.py')):
            text = path.read_text()
            for name in gone:
                self.assertIsNone(re.search(r'^\s*(import|from)\s+%s\b' % name, text, re.M), '%s imports %s' % (path.name, name))

    # R-src1 (acceptance author, WP4): the test above stays as it is. An import statement is itself the guarded thing,
    # and the text search also covers imports inside functions that no run can reach. This adds the behaviour:
    # none of the removed modules can be found, and importing every module of the program finds none of them either.
    def test_the_removed_modules_cannot_be_imported_and_no_module_needs_them(self):
        gone = ('room_experience', 'connections_ui', 'twitch_ui', 'world_radio_ui', 'room_discovery')
        script = '''
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
gone, missing = set(sys.argv[3:]), []
for name in sys.argv[2].split(','):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name in gone:
            missing.append((name, error.name))
    except BaseException:
        pass   # GTK without a display and similar: not what this oracle guards
print(json.dumps({'missing': missing, 'loaded': sorted(gone & set(sys.modules))}))
'''
        modules = sorted(path.stem for path in SOURCE.glob('*.py'))
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, XDG_STATE_HOME=home, XDG_CONFIG_HOME=home, XDG_CACHE_HOME=home)
            env.pop('DISPLAY', None)
            env.pop('WAYLAND_DISPLAY', None)
            result = subprocess.run([sys.executable, '-c', script, str(SOURCE), ','.join(modules)] + list(gone),
                                    capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = __import__('json').loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report, {'missing': [], 'loaded': []})
        for name in gone:
            self.assertIsNone(importlib.util.find_spec(name), name)


class HomeModuleContract(unittest.TestCase):
    """The desktop actions and Waybar settings point at verbs and flags that exist."""
    def setUp(self):
        # A desktop module (Home Manager or similar) that launches the app: set
        # LISTENING_HOME_MODULE=/path/to/module.nix to check its verbs and flags.
        module = os.environ.get('LISTENING_HOME_MODULE')
        if not module or not Path(module).exists():
            self.skipTest('LISTENING_HOME_MODULE is not set')
        module = Path(module)
        self.text = module.read_text()

    @unittest.skip('ORIGINAL, superseded by test_every_roomctl_verb_is_accepted_by_roomctl (R-src1, acceptance author, WP4): '
                   'it parsed the choices list out of roomctl.py source text, so a verb list built any other way would break it')
    def test_every_roomctl_verb_exists(self):
        source = (SOURCE / 'roomctl.py').read_text()
        choices = set(re.findall(r"'([a-z-]+)'", re.search(r"choices=\[(.*?)\]", source, re.S).group(1)))
        verbs = set(re.findall(r'listening-roomctl\s+([a-z-]+)', self.text))
        self.assertTrue({'return', 'menu', 'quiet', 'popover', 'status'} <= verbs, verbs)
        self.assertEqual(verbs - choices, set())

    def test_every_roomctl_verb_is_accepted_by_roomctl(self):
        # The real command line decides: an unknown verb is an argparse usage error (status 2) before --help runs.
        verbs = set(re.findall(r'listening-roomctl\s+([a-z-]+)', self.text))
        self.assertTrue({'return', 'menu', 'quiet', 'popover', 'status'} <= verbs, verbs)
        refused = []
        for verb in sorted(verbs | {'no-such-verb-wp4'}):
            result = subprocess.run([sys.executable, str(SOURCE / 'roomctl.py'), verb, '--help'], capture_output=True, text=True,
                                    timeout=60, stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONPATH=str(SOURCE)))
            if result.returncode != 0:
                refused.append(verb)
        self.assertEqual(refused, ['no-such-verb-wp4'])   # the control verb must be refused, every module verb accepted

    def test_every_listening_room_flag_is_accepted(self):
        if next_window is None:
            self.skipTest('GTK4 bindings unavailable')
        flags = set(re.findall(r'listening-room\s+(--[a-z-]+)', self.text))
        self.assertIn('--mini', flags)
        for flag in flags:
            next_window.launch_arguments([flag])   # raises ValueError for an unknown flag
        self.assertNotIn('--desk', flags)
        self.assertNotIn('classic', self.text)

    def test_the_waybar_left_click_is_the_popover_and_the_right_click_the_window(self):
        self.assertRegex(self.text, r'on-click\s*=\s*"\$\{hub\}/bin/listening-roomctl popover"')
        self.assertRegex(self.text, r'on-click-right\s*=\s*"\$\{hub\}/bin/listening-room"')

    def test_no_launcher_action_still_says_quiet_or_desk(self):
        self.assertNotIn('Desk mode', self.text)
        self.assertNotIn('Quiet room', self.text)


class StoredLaunchersStillWork(unittest.TestCase):
    def test_every_documented_flag_in_the_runbook_parses(self):
        if next_window is None:
            self.skipTest('GTK4 bindings unavailable')
        runbook = ROOT / 'docs/runbooks/listening-room-session.md'
        if not runbook.exists():
            self.skipTest('the runbook is not beside this test')
        text = runbook.read_text()
        for flag in set(re.findall(r'listening-room (--(?:mini|inside|preview-link|desk))\b', text)):
            argv = [flag] + (['https://example.org/a.mp3'] if flag == '--preview-link' else [])
            next_window.launch_arguments(argv)


class RetiredClassicTestsKeptTheirOracles(unittest.TestCase):
    """Behaviours the deleted classic tests asserted that the next window still has."""
    def test_unavailable_rooms_are_never_offered_and_surprise_skips_the_room_you_are_in(self):
        import random
        import next_rooms_model as rooms
        import room_catalogue
        catalogue = [dict(room, available=(room['id'] != 'night-drive')) for room in room_catalogue.rooms()]
        entries = rooms.entries({}, catalogue)
        self.assertNotIn('night-drive', [entry['id'] for entry in entries])
        for seed in range(25):
            pick = rooms.surprise(entries, 'all', {'active': {'id': 'after-hours'}}, random.Random(seed))
            self.assertNotIn(pick['id'], ('night-drive', 'after-hours'))

    def test_names_parse_like_the_old_pool_editor(self):
        import twitch_channels as twitch
        self.assertEqual(twitch.parse_names('LofiGirl, https://www.twitch.tv/lofigirl/; other_name'), ['lofigirl', 'other_name'])
        for value in ('https://twitch.tv/name?token=secret', 'https://other.org/name', 'https://user@twitch.tv/name'):
            with self.assertRaises(ValueError):
                twitch.parse_names(value)

    @unittest.skip('ORIGINAL, superseded by test_the_room_model_names_the_state_words (R-src1, acceptance author, WP4): '
                   'it only checked that a function called now_model exists, not what it says')
    def test_the_old_room_model_still_names_the_state_words(self):
        self.assertTrue(hasattr(next_model, 'now_model'))

    def test_the_room_model_names_the_state_words(self):
        from test_listening_next_model import snapshot
        self.assertIsNone(next_model.now_model(snapshot(active=None)))
        playing = snapshot()
        paused = snapshot()
        paused['layers']['music']['status'] = paused['layers']['atmosphere']['status'] = 'Paused'
        idle = snapshot()
        idle['layers']['music']['status'] = idle['layers']['atmosphere']['status'] = 'Stopped'
        self.assertEqual([next_model.now_model(s)['state'] for s in (playing, paused, idle)], ['playing', 'paused', 'idle'])
        self.assertEqual(next_model.now_model(playing)['name'], 'Midnight library')


if __name__ == '__main__':
    unittest.main()
