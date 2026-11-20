"""Independent acceptance oracles for WP3 (one mpv plumbing module, one HTTP helper, dead code out).

Owned by the acceptance author, not the implementer. Every expectation here was recorded by running
this same file against the baseline tree (0750cc4) with WP3_ORACLE_SRC pointing at its src/: the
expected wording, unit commands, timeouts and HTTP behaviour are the baseline's. Players are fake
(a fake mpv JSON-IPC server on a private Unix socket behind a fake `systemd-run`), HTTP goes to a
loopback server, and no real unit, audio or network is touched.

Exception: PictureWithoutHyprlandOracles cover the follow-up fix bc5a59a (a picture opens without
Hyprland; placement then says it needs Hyprland). Its no-Hyprland case fails on the baseline and on
848eaea by design; its two with-Hyprland cases pass there and pin that behaviour as unchanged.

Run: scripts/listening-dev.sh test test_listening_wp3_acceptance.py
"""
import contextlib
import http.server
import inspect
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

SRC = Path(os.environ.get('WP3_ORACLE_SRC') or Path(os.environ.get(
    'LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent)
sys.path.insert(0, str(SRC))
sys.path.insert(1, str(Path(__file__).resolve().parent))
os.environ.setdefault('LISTENING_HUB_CATALOG', str(SRC / 'catalogue.json'))

import listening_hub as hub  # noqa: E402
try:  # the baseline keeps the core inside listening_hub
    import player_core as core  # noqa: E402
except ImportError:
    core = hub
import ambience  # noqa: E402
import audio_layers  # noqa: E402
import provider_auth  # noqa: E402
import room_session  # noqa: E402
import twitch_channels  # noqa: E402
import twitch_credentials  # noqa: E402

UNIT = re.compile(r'listening-hub-radio-[0-9a-f]{12}\.service')


# --------------------------------------------------------------------------- fake mpv

class FakeMpv:
    """A fake `run` for unit commands; `systemd-run` starts a fake mpv IPC server.

    `mode` decides how the server answers loadfile: ok, rejected, closed, ended, timeout or start
    (start: the socket never appears).
    """

    def __init__(self, mode='ok', stop_rc=0, show='not-found\n'):
        self.mode, self.stop_rc, self.show = mode, stop_rc, show
        self.calls, self.ipc, self.threads, self.servers = [], [], [], []

    def run(self, args, check=True, **kwargs):
        args = list(args)
        self.calls.append((args, check))
        if args[0] == 'systemd-run':
            path = next(a.split('=', 1)[1] for a in args if a.startswith('--input-ipc-server='))
            if self.mode != 'start':
                self._serve(path)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[:3] == ['systemctl', '--user', 'stop']:
            return SimpleNamespace(returncode=self.stop_rc, stdout='', stderr='')
        if args[:3] == ['systemctl', '--user', 'show']:
            return SimpleNamespace(returncode=0, stdout=self.show, stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='')

    def unit_calls(self):
        return [(args, check) for args, check in self.calls if args[0] in ('systemd-run', 'systemctl')]

    def _serve(self, path):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(8)
        server.settimeout(5)
        self.servers.append(server)

        def accept():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                worker = threading.Thread(target=self._connection, args=(connection, path), daemon=True)
                worker.start()
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _connection(self, connection, path):
        def send(*messages):
            connection.sendall(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
        with connection, connection.makefile('rb') as reader:
            for line in reader:
                message = json.loads(line)
                command, identity = message['command'], message.get('request_id')
                self.ipc.append((Path(path).name, command))
                if command[0] != 'loadfile':
                    send({'request_id': identity, 'error': 'success', 'data': None})
                    continue
                if self.mode == 'ok':
                    send({'request_id': identity, 'error': 'success'}, {'event': 'start-file'},
                         {'event': 'end-file', 'reason': 'redirect'}, {'event': 'file-loaded'},
                         {'event': 'playback-restart'})
                elif self.mode == 'rejected':
                    send({'request_id': identity, 'error': 'loading\tfailed'})
                elif self.mode == 'closed':
                    return
                elif self.mode == 'ended':
                    send({'request_id': identity, 'error': 'success'},
                         {'event': 'end-file', 'reason': 'error', 'file_error': 'unrecognized\nfile format'})
                # timeout: say nothing

    def close(self):
        for server in self.servers:
            server.close()


@contextlib.contextmanager
def fast_clock(factor=400):
    """time.monotonic runs `factor` times faster, so fixed 12 s and 35 s load deadlines pass quickly."""
    real = time.monotonic
    start = real()
    with mock.patch('time.monotonic', lambda: start + (real() - start) * factor):
        yield


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / 'runtime').mkdir(mode=0o700)
        env = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(base / 'runtime'),
                                           'XDG_STATE_HOME': str(base / 'state'),
                                           'XDG_CONFIG_HOME': str(base / 'config'),
                                           'XDG_CACHE_HOME': str(base / 'cache')})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (('STATE', base / 'hub-state'), ('RUNTIME', base / 'runtime' / 'listening-hub'),
                            ('SOCKET', base / 'runtime' / 'listening-hub' / 'radio.sock')):
            patch = mock.patch.object(core, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch('audio_devices.player_options', return_value=[]),
                      mock.patch.object(core.shutil, 'which', side_effect=lambda name: '/fake/' + name)):
            patch.start()
            self.addCleanup(patch.stop)

    def fake(self, **kwargs):
        fake = FakeMpv(**kwargs)
        self.addCleanup(fake.close)
        return fake


# --------------------------------------------------------------------------- start_radio

class Event:
    """A cancel event that is never set and records each fade wait."""

    def __init__(self):
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)


def normalized(args):
    return [re.sub(r'radio-[0-9a-f]{12}', 'radio-ID', a) if isinstance(a, str) else a for a in args]


class StartRadioOracles(Runtime):
    def start(self, fake, name, fade=0.65, **kwargs):
        event = Event()
        with mock.patch.object(core, 'run', fake.run):
            core.start_radio({'id': name, 'name': name.title(), 'url': 'https://example.org/' + name},
                             cancel_event=event, fade_seconds=fade, **kwargs)
        return event

    def test_muted_candidate_then_commit_with_unique_units_and_the_fade(self):
        fake = self.fake()
        core.update_state(radio_audio={'volume': 40, 'muted': False})
        first = self.start(fake, 'one')
        first_unit = core.state()['radio_unit']
        launch = [normalized(args) for args, _ in fake.unit_calls() if args[0] == 'systemd-run']
        self.assertEqual(launch, [[
            'systemd-run', '--user', '--collect', '--unit=listening-hub-radio-ID.service',
            '--property=PartOf=graphical-session.target', '--property=After=graphical-session.target', '--',
            '/fake/mpv', '--no-config', '--idle=yes', '--no-video', '--input-terminal=no',
            '--audio-client-name=ListeningRoomMusic',
            '--input-ipc-server=' + str(core.RUNTIME / 'radio-ID.sock'), '--volume=0', '--mute=yes', '--ytdl=no']])
        self.assertRegex(first_unit, UNIT)
        # Fade: 13 steps of 0.65/13 s up to the remembered level, after the muted preflight.
        self.assertEqual(len(first.waits), 13)
        self.assertAlmostEqual(sum(first.waits), 0.65)
        candidate = [command for sock, command in fake.ipc if command[0] != 'loadfile']
        self.assertEqual(candidate[:3], [['set_property', 'volume', 0], ['set_property', 'mute', True],
                                         ['set_property', 'mute', False]])
        volumes = [command[2] for command in candidate[3:]]
        self.assertEqual(len(volumes), 13)
        self.assertAlmostEqual(volumes[0], 40 / 13)
        self.assertAlmostEqual(volumes[-1], 40)

        fake.calls.clear()
        fake.ipc.clear()
        self.start(fake, 'two')
        second_unit = core.state()['radio_unit']
        self.assertRegex(second_unit, UNIT)
        self.assertNotEqual(first_unit, second_unit)
        # The old unit is stopped only after the new candidate loaded and answered its muted preflight.
        units = fake.unit_calls()
        self.assertEqual(units[0][0][3], '--unit=' + second_unit)
        self.assertEqual(units[1:], [(['systemctl', '--user', 'stop', first_unit], False)])
        self.assertEqual(core.state()['radio_source_id'], 'two')

    def test_start_lock_serialises_starts(self):
        fake = self.fake()
        core.RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        import fcntl
        lock = os.open(core.RUNTIME / 'radio-start.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        cancel = threading.Event()
        errors = []

        def start():
            try:
                with mock.patch.object(core, 'run', fake.run):
                    core.start_radio({'id': 'x', 'name': 'X', 'url': 'https://example.org/x'}, cancel_event=cancel)
            except core.HubError as error:
                errors.append(str(error))
        worker = threading.Thread(target=start)
        worker.start()
        time.sleep(.4)
        self.assertEqual(fake.calls, [], 'a start must wait for the start lock')
        cancel.set()
        worker.join(3)
        self.assertEqual(errors, ['Radio start cancelled'])
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.stat(core.RUNTIME / 'radio-start.lock').st_mode & 0o777, 0o600)

    def test_session_starts_music_with_the_065_fade(self):
        backend = room_session.Backend()
        spy = mock.create_autospec(core.start_radio)
        cancel = threading.Event()
        with mock.patch.object(hub, 'start_radio', spy), mock.patch.object(hub, 'radio_info', return_value=None):
            backend.start_music({'id': 's', 'name': 'S', 'kind': 'radio', 'url': 'https://example.org/s'}, cancel)
        self.assertEqual(spy.call_args.kwargs, {'cancel_event': cancel, 'fade_seconds': .65})
        with tempfile.TemporaryFile() as audio, mock.patch.object(hub, 'start_radio', spy):
            backend.start_verified_music({'reference': {'id': 'r', 'name': 'R'}}, cancel, audio)
        self.assertEqual(spy.call_args.kwargs['fade_seconds'], .65)


# --------------------------------------------------------------------------- load failure wording

RADIO = {
    'start': 'Could not start radio: Radio player did not start; check the user session Previous playback has been preserved.',
    'rejected': 'Could not start radio: Unable to load radio: loading failed Previous playback has been preserved.',
    'closed': 'Could not start radio: Radio player closed before stream loaded Previous playback has been preserved.',
    'ended': 'Could not start radio: Radio stream could not be opened: unrecognized file format Previous playback has been preserved.',
    'timeout': 'Could not start radio: Radio loading timed out; previous playback has been preserved Previous playback has been preserved.'}
LAYER = {
    'start': 'Audio layer player did not start Previous background playback has been preserved.',
    'rejected': 'Audio layer could not load the stream Previous background playback has been preserved.',
    'closed': 'Audio layer closed while loading Previous background playback has been preserved.',
    'ended': 'Audio layer could not play this stream; try Twitch in a browser Previous background playback has been preserved.',
    'timeout': 'Audio layer loading timed out; try Twitch in a browser Previous background playback has been preserved.'}
PICTURE = {
    'start': 'The video window did not open',
    'rejected': 'The video URL could not be loaded',
    'closed': 'The video window closed while loading',
    'ended': 'This video could not be played. Try the offline lake, a local clip, or another public URL.',
    'timeout': 'Video loading timed out. Try an offline vista or another URL.'}


class LoadFailureOracles(Runtime):
    def candidate(self, fake):
        launch = next(args for args, _ in fake.calls if args[0] == 'systemd-run')
        return next(a.split('=', 1)[1] for a in launch if a.startswith('--unit='))

    def test_radio_wording_per_reason_and_only_the_candidate_stops(self):
        for reason, expected in RADIO.items():
            with self.subTest(reason=reason):
                core.update_state(radio_unit='listening-hub-radio-000000000000.service', radio_source_id='old')
                fake = self.fake(mode=reason)
                with mock.patch.object(core, 'run', fake.run), fast_clock():
                    with self.assertRaises(core.HubError) as caught:
                        core.start_radio({'id': 'n', 'name': 'N', 'url': 'https://example.org/n'})
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(fake.unit_calls()[1:], [(['systemctl', '--user', 'stop', self.candidate(fake)], False)])
                self.assertEqual(core.state()['radio_source_id'], 'old')

    def test_layer_wording_per_reason(self):
        for reason, expected in LAYER.items():
            with self.subTest(reason=reason):
                fake = self.fake(mode=reason)
                with mock.patch.object(audio_layers, '_run', fake.run), mock.patch.object(audio_layers, 'state', return_value=None), \
                        mock.patch.object(audio_layers.shutil, 'which', side_effect=lambda name: '/fake/' + name), fast_clock(40):
                    with self.assertRaises(audio_layers.LayerError) as caught:
                        audio_layers.start_layer('https://www.twitch.tv/name', timeout=12)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(fake.unit_calls()[-1], (['systemctl', '--user', 'stop', self.candidate(fake)], False))
        fake = self.fake(mode='closed', stop_rc=1)
        with mock.patch.object(audio_layers, '_run', fake.run), mock.patch.object(audio_layers, 'state', return_value=None), \
                mock.patch.object(audio_layers.shutil, 'which', side_effect=lambda name: '/fake/' + name):
            with self.assertRaisesRegex(audio_layers.LayerError, r'^Audio layer closed while loading The audio layer could not be stopped\.$'):
                audio_layers.start_layer('https://www.twitch.tv/name', timeout=12)

    def test_picture_wording_per_reason(self):
        for reason, expected in PICTURE.items():
            with self.subTest(reason=reason):
                fake = self.fake(mode=reason)
                with mock.patch.object(ambience, '_run', fake.run), \
                        mock.patch.object(ambience.shutil, 'which', side_effect=lambda name: '/fake/' + name), fast_clock():
                    with self.assertRaises(ambience.AmbienceError) as caught:
                        ambience.start('https://example.org/clip.mp4', mode='ambience')
                self.assertEqual(str(caught.exception), expected)
                units = fake.unit_calls()
                self.assertEqual(units[0], (['systemctl', '--user', 'stop', 'listening-hub-ambience.service'], False))
                self.assertEqual(units[1][0][:4], ['systemd-run', '--user', '--collect', '--unit=listening-hub-ambience.service'])
                self.assertEqual(units[2:], [(['systemctl', '--user', 'stop', 'listening-hub-ambience.service'], False)])
        fake = self.fake(mode='closed', stop_rc=1, show='loaded\n')
        with mock.patch.object(ambience, '_run', fake.run), \
                mock.patch.object(ambience.shutil, 'which', side_effect=lambda name: '/fake/' + name):
            with self.assertRaisesRegex(ambience.AmbienceError, 'existing window could not be stopped'):
                ambience.start('https://example.org/clip.mp4', mode='ambience')
        self.assertEqual([args for args, _ in fake.unit_calls()], [
            ['systemctl', '--user', 'stop', 'listening-hub-ambience.service'],
            ['systemctl', '--user', 'show', 'listening-hub-ambience.service', '--property=LoadState', '--value']])


class PictureWithoutHyprlandOracles(Runtime):
    """Follow-up bc5a59a: a picture opens without Hyprland and placement says it needs it."""

    def start(self, hyprctl):
        fake = self.fake()

        def run(args, **kwargs):
            if args[0] == 'hyprctl':
                return hyprctl(args)
            return fake.run(args, **{k: v for k, v in kwargs.items() if k == 'check'})
        calls = []

        def recording(args, **kwargs):
            calls.append(list(args))
            return run(args, **kwargs)
        patches = (mock.patch.object(ambience.subprocess, 'run', recording),
                   mock.patch.object(ambience, 'STATE_HOME', Path(self.tmp.name) / 'picture-state'),
                   mock.patch.object(ambience.shutil, 'which', side_effect=lambda name: '/fake/' + name))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            began = time.monotonic()
            try:
                result = ambience.start('https://example.org/clip.mp4', mode='ambience', placement='pin')
            except ambience.AmbienceError as error:
                result = error
            elapsed = time.monotonic() - began
            try:
                ambience.place('here', 'ambience')
                placed = None
            except ambience.AmbienceError as error:
                placed = str(error)
        return result, elapsed, placed, calls

    def test_start_succeeds_quickly_and_placement_explains_without_hyprctl(self):
        def missing(args):
            raise FileNotFoundError(2, 'No such file or directory', 'hyprctl')

        def failing(args):
            return SimpleNamespace(returncode=1, stdout='', stderr='HYPRLAND_INSTANCE_SIGNATURE not set')

        def garbage(args):
            return SimpleNamespace(returncode=0, stdout='not json', stderr='')
        for name, hyprctl in (('missing', missing), ('failing', failing), ('garbage', garbage)):
            with self.subTest(hyprctl=name):
                result, elapsed, placed, calls = self.start(hyprctl)
                self.assertIsInstance(result, dict, result)
                self.assertLess(elapsed, 1.5, 'must not wait out the 3 s mapping deadline')
                self.assertEqual([c for c in calls if c[:2] == ['hyprctl', 'dispatch']], [])
                self.assertEqual(placed, 'Placement needs Hyprland')

    def test_with_hyprland_the_start_still_places_the_window(self):
        client = {'class': ambience.APP_IDS['ambience'], 'address': '0x321', 'workspace': {'id': 2}}

        def hyprland(args):
            if args[1] == 'clients':
                return SimpleNamespace(returncode=0, stdout=json.dumps([client]), stderr='')
            if args[1] == 'activeworkspace':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'id': 2}), stderr='')
            return SimpleNamespace(returncode=0, stdout='ok', stderr='')
        result, elapsed, placed, calls = self.start(hyprland)
        self.assertIsInstance(result, dict, result)
        self.assertIsNone(placed)
        self.assertTrue([c for c in calls if c[:2] == ['hyprctl', 'dispatch'] and 'pin' in c[2]])

    def test_with_hyprland_but_no_window_yet_the_mapping_wait_remains(self):
        def empty(args):
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')
        result, elapsed, placed, calls = self.start(empty)
        self.assertIsInstance(result, ambience.AmbienceError)
        self.assertEqual(str(result), 'Open a video window first')
        self.assertGreaterEqual(elapsed, 2.9)
        self.assertEqual(placed, 'Open a video window first')


# --------------------------------------------------------------------------- stop commands

class StopOracles(Runtime):
    def test_radio_stop_is_one_checked_systemctl_stop(self):
        core.update_state(radio_unit='listening-hub-radio-0123456789ab.service')
        fake = self.fake()
        with mock.patch.object(core, 'run', fake.run):
            room_session.Backend().music_control('stop')
        self.assertEqual(fake.unit_calls(), [(['systemctl', '--user', 'stop', 'listening-hub-radio-0123456789ab.service'], True)])

    def test_layer_stop_checks_load_state_after_a_failed_stop(self):
        for show, failed in (('not-found\n', False), ('loaded\n', True)):
            with self.subTest(show=show):
                fake = self.fake(stop_rc=1, show=show)
                with mock.patch.object(audio_layers, '_run', fake.run):
                    if failed:
                        with self.assertRaises(audio_layers.LayerError):
                            audio_layers.control('stop')
                    else:
                        audio_layers.control('stop')
                self.assertEqual([args for args, _ in fake.unit_calls()], [
                    ['systemctl', '--user', 'stop', audio_layers.UNIT],
                    ['systemctl', '--user', 'show', audio_layers.UNIT, '--property=LoadState', '--value']])
                self.assertEqual({check for _, check in fake.unit_calls()}, {False})

    def test_picture_stop_is_checked(self):
        fake = self.fake()
        with mock.patch.object(ambience, '_run', fake.run):
            ambience.control('stop', 'ambience')
        self.assertEqual(fake.unit_calls(), [(['systemctl', '--user', 'stop', 'listening-hub-ambience.service'], True)])

    def test_orphaned_verified_player_stop(self):
        unit = 'listening-hub-radio-0123456789ab.service'
        for stop_rc, show, raises in ((0, '', False), (1, 'not-found\n', False), (1, 'loaded\n', True)):
            with self.subTest(stop_rc=stop_rc, show=show):
                fake = self.fake(stop_rc=stop_rc, show=show)
                with mock.patch.object(hub, 'run', fake.run), mock.patch.object(core, 'run', fake.run):
                    if raises:
                        with self.assertRaisesRegex(room_session.SessionError, 'Could not stop an orphaned verified player'):
                            room_session.Backend().stop_owned_identity(unit)
                    else:
                        room_session.Backend().stop_owned_identity(unit)
                expected = [(['systemctl', '--user', 'stop', unit], False)]
                if stop_rc:
                    expected.append((['systemctl', '--user', 'show', unit, '--property=LoadState', '--value'], False))
                self.assertEqual(fake.unit_calls(), expected)


class QueueBypassOracles(unittest.TestCase):
    def test_stop_and_quiet_reach_players_while_an_entry_is_still_loading(self):
        from test_listening_session import FakeBackend
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {
                'XDG_CONFIG_HOME': folder + '/config', 'XDG_STATE_HOME': folder + '/state', 'XDG_CACHE_HOME': folder + '/cache'}):
            backend = FakeBackend()
            controller = room_session.Controller(backend, folder, clock=lambda: 1000.)
            self.addCleanup(controller.close)
            sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'}]
            rooms = [{'id': 'library', 'name': 'Library', 'source_id': 'piano', 'view_id': 'room-library'}]
            with mock.patch.object(room_session.hub, 'sources', return_value=sources), \
                    mock.patch.object(room_session.room_catalogue, 'rooms', return_value=rooms), \
                    mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'):
                backend.block = threading.Event()
                controller.enter({'room_id': 'library'})
                self.assertTrue(backend.started.wait(2))
                began = time.monotonic()
                controller.dispatch('quiet', {})
                controller.dispatch('stop', {})
                self.assertLess(time.monotonic() - began, 1, 'Quiet/Stop must not wait behind a loading entry')
                self.assertIn(('music', 'stop', None), backend.calls)
                backend.block.set()
                controller.worker.submit(lambda: None).result(timeout=3)
            self.assertNotIn(('start_music', 'piano'), backend.calls)


# --------------------------------------------------------------------------- HTTP

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        self.server.paths.append(self.path)
        if 'redirect' in self.path or 'Authenticate' in self.path:
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:%d/target' % self.server.server_port)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        size = int(self.path.rsplit('/', 1)[-1]) if self.path.startswith('/size/') else 2
        body = b'"' + b'a' * (size - 2) + b'"' if size >= 2 else b'1'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer


LIMIT = 2 * 1024 * 1024


class HttpOracles(unittest.TestCase):
    def setUp(self):
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.paths = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        # A dead proxy: a client that honours proxy settings cannot reach the loopback server.
        dead = socket.socket()
        dead.bind(('127.0.0.1', 0))
        port = dead.getsockname()[1]
        dead.close()
        env = mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:%d' % port,
                                           'HTTP_PROXY': 'http://127.0.0.1:%d' % port})
        env.start()
        self.addCleanup(env.stop)
        for name in ('no_proxy', 'NO_PROXY'):
            os.environ.pop(name, None)

    @contextlib.contextmanager
    def openers(self):
        """Record each opener's handlers and timeout while letting the request through."""
        seen = []
        real_build, real_open = urllib.request.build_opener, urllib.request.OpenerDirector.open

        def build(*handlers):
            opener = real_build(*handlers)
            opener._wp3 = {'proxies': next((h.proxies for h in handlers if isinstance(h, urllib.request.ProxyHandler)), None),
                           'redirect': [type(h).__name__ for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]}
            return opener

        def open_(self, request, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
            seen.append(dict(getattr(self, '_wp3', {}), timeout=timeout))
            return real_open(self, request, data, timeout)
        with mock.patch.object(urllib.request, 'build_opener', build), \
                mock.patch.object(urllib.request.OpenerDirector, 'open', open_):
            yield seen

    def test_twitch_api_keeps_timeout_size_limit_no_redirect_and_proxy_settings(self):
        with self.openers() as seen, mock.patch.dict(os.environ, {'no_proxy': '127.0.0.1', 'NO_PROXY': '127.0.0.1'}):
            self.assertEqual(twitch_channels._json_get(urllib.request.Request(self.base + '/size/%d' % LIMIT)), 'a' * (LIMIT - 2))
            with self.assertRaisesRegex(ValueError, 'oversized'):
                twitch_channels._json_get(urllib.request.Request(self.base + '/size/%d' % (LIMIT + 1)))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                twitch_channels._json_get(urllib.request.Request(self.base + '/redirect'))
        self.assertEqual(caught.exception.code, 302)
        self.assertNotIn('/target', self.server.paths)
        self.assertEqual({entry['timeout'] for entry in seen}, {5})
        self.assertEqual({entry['proxies'] for entry in seen if 'proxies' in entry}, {None}, 'Twitch API honours proxy settings')
        # And with a proxy configured, it goes through it (here: a dead one).
        with self.assertRaises(OSError):
            twitch_channels._json_get(urllib.request.Request(self.base + '/size/2'))

    def test_twitch_renewal_bypasses_proxies_and_refuses_redirects(self):
        with self.openers() as seen, mock.patch.object(twitch_credentials, 'TOKEN_ENDPOINT', self.base + '/redirect'):
            with self.assertRaises(twitch_credentials.CredentialError) as caught:
                twitch_credentials._renew({'CLIENTID': 'test-client', 'REFRESHTOKEN': 'test-refresh'})
        self.assertEqual(str(caught.exception), 'Twitch renewal refused a redirect; retry discovery')
        self.assertEqual(self.server.paths, ['/redirect'], 'reached the server directly despite the dead proxy, no redirect followed')
        self.assertEqual([(e['proxies'], e['timeout']) for e in seen], [({}, 5)])

    def test_device_authorization_bypasses_proxies_with_its_timeout(self):
        with self.openers() as seen, mock.patch('urllib.request.HTTPSHandler.https_open', side_effect=urllib.error.URLError('offline')):
            with self.assertRaises(Exception):
                provider_auth._post('device', {'client_id': 'test-client'})
        self.assertEqual([(e['proxies'], e['timeout']) for e in seen], [({}, 6)])
        handler = provider_auth._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://other.example/'))

    def test_jellyfin_sign_in_refuses_redirects_with_its_wording(self):
        import online_services
        with self.openers() as seen, mock.patch.dict(os.environ, {'no_proxy': '127.0.0.1', 'NO_PROXY': '127.0.0.1'}), \
                mock.patch.object(online_services, 'config', return_value={'jellyfin': {'endpoint': self.base}}):
            with self.assertRaises(online_services.ConnectionError) as caught:
                online_services.jellyfin_login('test-user', 'test-password')
        self.assertEqual(str(caught.exception), 'Jellyfin sign-in could not complete; check the server and retry')
        self.assertEqual(self.server.paths, ['/Users/AuthenticateByName'])
        self.assertNotIn('/target', self.server.paths)
        self.assertEqual([(e['proxies'], e['timeout']) for e in seen], [(None, 6)])


# --------------------------------------------------------------------------- module split and dead code

# Every public name of the baseline listening_hub that any module, roomctl, test or probe reads through it.
USED_THROUGH_HUB = [
    'CATALOG', 'HubError', 'RUNTIME', 'SOCKET', 'STATE', 'all_info', 'ambience_module', 'choose', 'clean',
    'ensure_baseline', 'favorites', 'inspect_state', 'ipc', 'load_json', 'main', 'menu', 'mute', 'notify', 'play',
    'play_scene', 'playback_url', 'player_command', 'player_volume', 'players', 'preferences', 'private_favorite',
    'profiles', 'radio_audio_state', 'radio_info', 'radio_socket', 'radio_unit', 'restore_profile', 'run',
    'save_favorite', 'save_json', 'save_profile', 'save_scene', 'scenes', 'search_radio', 'select_player',
    'selected_target', 'set_volume', 'sources', 'start_radio', 'state', 'station_actions', 'station_menu', 'status',
    'system_volume', 'target', 'transport', 'update_state', 'valid_url', 'volume_value', 'youtube_video_url']


class ModuleOracles(unittest.TestCase):
    def test_public_names_still_import_from_listening_hub(self):
        self.assertEqual([name for name in USED_THROUGH_HUB if not hasattr(hub, name)], [])
        signature = inspect.signature(hub.start_radio).parameters
        for name in ('cancel_event', 'fade_seconds', 'mpv_options', 'trusted_fd', 'trusted_unit', 'ytdlp'):
            self.assertIn(name, signature)

    def test_room_session_has_no_gtk_or_dbus_import(self):
        code = ('import sys; import room_session; '
                'print(sorted({m.split(".")[0] for m in sys.modules} & {"gi", "dbus", "pydbus", "jeepney", "dbus_next"}))')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, PYTHONPATH=str(SRC)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_cli_entry_points_answer_help_offline(self):
        for module in ('listening_hub.py', 'roomctl.py'):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as home:
                result = subprocess.run([sys.executable, str(SRC / module), '--help'], capture_output=True, text=True, timeout=60,
                                        env=dict(os.environ, HOME=home, XDG_STATE_HOME=home, XDG_RUNTIME_DIR=home,
                                                 PYTHONPATH=str(SRC)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
