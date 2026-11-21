"""Independent acceptance oracles for O10-YOUTUBE: Watch with sound, Show as scene, YouTube shelves.

Owned by the acceptance author, not the implementer. Written from the brief (O10-YOUTUBE criteria 1, 2, 4,
5, 6) and UX assessment Proposals B and C, not from the implementation.

The real session controller runs the room layers on the session test double (FakeBackend) and everything
else on the real `room_session.Backend`, so a `video` action reaches the real `ambience` module. Every
process start goes through a fake `subprocess.run`: `systemd-run` starts a fake mpv JSON-IPC server on a
private Unix socket, `systemctl --user stop` ends it, anything else fails. Nothing is downloaded or played.

Adversarial readings each test guards against:
- Watch built as "audio in the Music layer plus picture in the Scene" (two players, no shared clock);
- Watch that starts the room pause after the video, so Quiet's own pause also pauses the new video;
- Stop watching (or the window closing) that resumes the room by itself;
- Watch with no room that invents a room, a pause or a resume offer;
- Show as scene that starts sound (an mpv with audio, the video window, or a music/ambience change);
- a half-built account path (cookies, :ytrec, subscriptions).

Run: scripts/listening-dev.sh test test_listening_o10y_oracles.py
"""
import contextlib
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

SRC = Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SRC))
sys.path.insert(1, str(Path(__file__).resolve().parent))

import ambience  # noqa: E402
import room_session as sessions  # noqa: E402
import session_service  # noqa: E402
from test_listening_session import FakeBackend  # noqa: E402

VIDEO_ID = 'o10yWatch01'
VIDEO_URL = 'https://www.youtube.com/watch?v=' + VIDEO_ID
VIDEO_TITLE = 'Rain on a library window'
VIDEO_UNIT = 'listening-hub-video.service'
PICTURE_UNIT = 'listening-hub-ambience.service'
REAL_RUN = subprocess.run


class FakeProcesses:
    """A fake `subprocess.run`: systemd-run starts a fake mpv IPC server, systemctl stop ends it."""

    def __init__(self):
        self.calls, self.ipc, self.players = [], [], {}
        self.lock = threading.Lock()

    def spawned(self, unit=None):
        return [args for args in self.calls if args[:1] == ['systemd-run']
                and (unit is None or '--unit=' + unit in args)]

    def __call__(self, args, *pos, **kwargs):
        if not isinstance(args, (list, tuple)):
            return REAL_RUN(args, *pos, **kwargs)
        args = [str(a) for a in args]
        with self.lock:
            self.calls.append(args)
        if args[:1] == ['systemd-run']:
            unit = next(a.split('=', 1)[1] for a in args if a.startswith('--unit='))
            path = next(a.split('=', 1)[1] for a in args if a.startswith('--input-ipc-server='))
            self._serve(unit, path)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[:3] == ['systemctl', '--user', 'stop']:
            self.end(args[3])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[:3] == ['systemctl', '--user', 'show']:
            return SimpleNamespace(returncode=0, stdout='not-found\n', stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='not available in the oracle')

    def player(self, unit):
        return self.players.get(unit)

    def end(self, unit):
        """The window closed or the video ended: the player and its socket are gone."""
        player = self.players.pop(unit, None)
        if player:
            player['server'].close()
            Path(player['path']).unlink(missing_ok=True)

    def close(self):
        for unit in list(self.players):
            self.end(unit)

    def _serve(self, unit, path):
        self.end(unit)
        Path(path).unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(8)
        player = {'server': server, 'path': path, 'idle': True, 'pause': False, 'url': None,
                  'volume': 100, 'mute': False}
        self.players[unit] = player

        def accept():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                threading.Thread(target=self._connection, args=(connection, unit, player), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()

    def _connection(self, connection, unit, player):
        def send(*messages):
            connection.sendall(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
        try:
            with connection, connection.makefile('rb') as reader:
                for line in reader:
                    message = json.loads(line)
                    command, identity = message.get('command') or [''], message.get('request_id')
                    self.ipc.append((unit, command))
                    name = command[0]
                    if name == 'loadfile':
                        player.update(idle=False, url=command[1], pause=False)
                        send({'request_id': identity, 'error': 'success'}, {'event': 'start-file'},
                             {'event': 'file-loaded'}, {'event': 'playback-restart'})
                        continue
                    data = None
                    if name == 'get_property':
                        data = {'idle-active': player['idle'], 'pause': player['pause'],
                                'media-title': VIDEO_TITLE if player['url'] else None, 'path': player['url'],
                                'volume': player['volume'], 'mute': player['mute']}.get(command[1])
                    elif name == 'set_property' and command[1] in ('pause', 'volume', 'mute'):
                        player[command[1]] = command[2]
                    elif name == 'cycle' and command[1:2] == ['pause']:
                        player['pause'] = not player['pause']
                    elif name in ('stop', 'quit'):
                        player.update(idle=True, url=None)
                    send({'request_id': identity, 'error': 'success', 'data': data})
        except OSError:
            pass


class VideoBackend(FakeBackend, sessions.Backend):
    """Room layers on the session double; anything new (the video window) on the real Backend."""

    def __init__(self):
        FakeBackend.__init__(self)
        sessions.Backend.__init__(self)

    def extra_video_control(self, action):
        self.calls.append(('owned_video', action))
        return sessions.Backend.extra_video_control(self, action)

    def layers(self):
        try:
            real = dict(sessions.Backend.layers(self))
        except Exception:
            real = {}
        real.update(FakeBackend.layers(self))
        return real


class RealPictureBackend(VideoBackend):
    """Also the real picture path, so Show as scene really reaches `ambience.start`."""
    start_picture = sessions.Backend.start_picture


ROOM_STATES = ('music', 'atmosphere')
EFFECTS = ('start_music', 'start_verified_music', 'start_atmosphere', 'start_picture')


class Oracle(unittest.TestCase):
    backend_class = VideoBackend

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
        for name in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE'):
            os.environ.pop(name, None)
        self.processes = FakeProcesses()
        self.addCleanup(self.processes.close)
        self.backend = self.backend_class()
        self.now = 1000.
        self.controller = sessions.Controller(self.backend, str(base / 'controller'), clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        self.service = session_service.Service(self.controller)
        self.sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'}]
        self.rooms = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library',
                       'poster_path': '/artwork/library.jpg', 'accent': '#c79b68'}]
        for patch in (mock.patch('subprocess.run', self.processes),
                      mock.patch.object(ambience, 'STATE_HOME', base / 'state' / 'listening-hub'),
                      mock.patch('audio_devices.player_options', return_value=[]),
                      mock.patch.object(ambience.shutil, 'which',
                                        side_effect=lambda name: '/fake/' + name if name in ('mpv', 'yt-dlp') else None),
                      mock.patch.object(sessions.hub, 'sources', return_value=self.sources),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=self.rooms),
                      mock.patch.object(self.controller, '_home_action'),
                      mock.patch.object(self.controller, '_route_preferred')):
            patch.start()
            self.addCleanup(patch.stop)

    # ---- helpers --------------------------------------------------------------------------------------------
    def settle(self, refreshes=1):
        for _ in range(refreshes):
            self.controller.worker.submit(lambda: None).result(timeout=10)
            self.controller.refresh()

    def enter_room(self):
        self.controller.enter({'room_id': 'library',
                               'atmosphere': {'kind': 'local', 'source_id': 'rain', 'volume': 18, 'muted': False}})
        self.settle()
        for layer in ROOM_STATES:
            self.assertEqual(self.backend.state[layer]['status'], 'Playing', 'fixture room did not start ' + layer)
        self.backend.calls.clear()

    def action(self, name, args):
        """One controller request as the window sends it; queued work is drained."""
        try:
            result = self.service.execute(name, args)
        finally:
            self.settle()
        return result

    def start_video(self, url=VIDEO_URL, name=VIDEO_TITLE):
        """`video` start. The brief fixes the action name and start/stop, not the argument names: the
        oracle accepts either a flat url/name or an item, and fails if neither starts a video window."""
        if 'video' not in sessions.ACTIONS:
            self.fail('the controller has no `video` action (criterion 5)')
        for args in ({'action': 'start', 'url': url, 'name': name},
                     {'action': 'start', 'item': {'url': url, 'name': name}}):
            try:
                self.action('video', args)
            except sessions.SessionError:
                continue
            if self.processes.spawned(VIDEO_UNIT):
                return args
        self.fail('video start did not open the video window: %r; errors %r'
                  % (self.processes.calls, self.controller.snapshot().get('errors')))

    def stop_video(self):
        return self.action('video', {'action': 'stop'})

    def effects(self):
        return [c for c in self.backend.calls if c[0] in EFFECTS]

    def controls(self, layer, action=None):
        return [c for c in self.backend.calls if c[0] == layer and (action is None or c[1] == action)]

    def video_mentions(self, snapshot):
        """Dicts in the snapshot, outside the room recipe and its history, that describe this video."""
        found = []

        def visit(value, path):
            if isinstance(value, dict):
                text = json.dumps(value, default=str)
                if (VIDEO_ID in text or VIDEO_TITLE in text) and not any(isinstance(v, (dict, list)) and (
                        VIDEO_ID in json.dumps(v, default=str) or VIDEO_TITLE in json.dumps(v, default=str))
                        for v in value.values()):
                    found.append((path, value))
                for key, item in value.items():
                    visit(item, path + (key,))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, path + (index,))
        for key, value in snapshot.items():
            if key not in ('active', 'last_session', 'recent', 'stations', 'station_recent', 'variants', 'station'):
                visit(value, (key,))
        return found


class WatchOracles(Oracle):
    """Criteria 1, 5, 6: Watch is one player window with sound, around a Quiet-style room pause."""

    def test_watch_is_one_video_window_with_sound_not_a_music_layer_plus_picture(self):
        self.enter_room()
        recipe = json.dumps({k: self.controller.active.get(k) for k in ('music', 'atmosphere', 'picture')}, sort_keys=True)
        self.start_video()
        launches = self.processes.spawned()
        self.assertEqual(len(launches), 1, 'exactly one player starts for Watch: %r' % launches)
        argv = launches[0]
        self.assertIn('--unit=' + VIDEO_UNIT, argv)
        self.assertIn('--wayland-app-id=' + ambience.APP_IDS['video'], argv)
        self.assertNotIn('--no-audio', argv, 'Watch is with sound')
        self.assertNotIn('--aid=no', argv, 'Watch is with sound')
        self.assertTrue(any(a.startswith('--ytdl-format=') and 'bestaudio' in a for a in argv), argv)
        loaded = [c for u, c in self.processes.ipc if u == VIDEO_UNIT and c[0] == 'loadfile']
        self.assertEqual(len(loaded), 1, loaded)
        self.assertIn(VIDEO_ID, loaded[0][1])
        self.assertEqual(self.effects(), [], 'Watch must not start music, ambience or a scene')
        self.assertEqual(self.controls('picture', 'stop'), [], 'Watch keeps the Scene')
        self.assertEqual(json.dumps({k: self.controller.active.get(k) for k in ('music', 'atmosphere', 'picture')},
                                    sort_keys=True), recipe, 'Watch must not change the room recipe')

    def test_watch_pauses_the_room_like_quiet_with_a_resume_offer_and_the_video_keeps_playing(self):
        self.enter_room()
        self.start_video()
        self.settle(2)
        snapshot = self.controller.snapshot()
        for layer in ROOM_STATES:
            self.assertTrue(self.controls(layer, 'pause'), layer + ' was not paused')
            self.assertEqual(snapshot['layers'][layer]['status'], 'Paused', layer)
        self.assertIsNotNone(snapshot['resume_offer'], 'Watch records the resume offer as Quiet does')
        self.assertTrue(set(ROOM_STATES) <= set(snapshot['resume_offer'].get('layers') or []), snapshot['resume_offer'])
        player = self.processes.player(VIDEO_UNIT)
        self.assertIsNotNone(player, 'the video window is open')
        self.assertFalse(player['pause'], 'the room pause must not pause the video it was made for')

    def test_snapshot_reports_the_active_video_and_forgets_it_after_stop(self):
        self.assertEqual(self.video_mentions(self.controller.snapshot()), [], 'fixture: no video before Watch')
        self.enter_room()
        self.start_video()
        self.settle(2)
        playing = self.video_mentions(self.controller.snapshot())
        self.assertTrue(playing, 'the snapshot has fields for the active video (title or url)')
        self.assertTrue(any(str(v.get('status', '')).lower() in ('playing', 'watching') or v.get('active') is True
                            or v.get('playing') is True for _p, v in playing),
                        'the active video is reported as playing: %r' % playing)
        self.stop_video()
        self.settle(2)
        still = [(p, v) for p, v in self.video_mentions(self.controller.snapshot())
                 if str(v.get('status', '')).lower() in ('playing', 'paused', 'watching') or v.get('active') is True]
        self.assertEqual(still, [], 'after Stop watching no video is reported as active')

    def test_stop_watching_closes_the_window_and_never_auto_resumes(self):
        self.enter_room()
        self.start_video()
        self.backend.calls.clear()
        self.stop_video()
        self.assertIn(['systemctl', '--user', 'stop', VIDEO_UNIT], self.processes.calls)
        self.assertIsNone(self.processes.player(VIDEO_UNIT))
        for _ in range(3):
            self.now += 30
            self.settle()
        snapshot = self.controller.snapshot()
        self.assertEqual([c for c in self.backend.calls if c[1:2] == ('play',)], [], 'resume is always explicit')
        for layer in ROOM_STATES:
            self.assertEqual(snapshot['layers'][layer]['status'], 'Paused', layer)
        self.assertIsNotNone(snapshot['resume_offer'], 'stopping offers Resume')
        self.assertIsNotNone(snapshot['active'], 'stopping the video does not leave the room')
        self.action('resume', {})
        self.assertEqual(self.backend.state['music']['status'], 'Playing', 'an explicit Resume still works')

    def test_window_closed_or_video_ended_offers_resume_without_resuming(self):
        self.enter_room()
        self.start_video()
        self.settle(2)
        self.backend.calls.clear()
        self.processes.end(VIDEO_UNIT)
        for _ in range(4):
            self.now += 5
            self.settle()
        snapshot = self.controller.snapshot()
        self.assertEqual([c for c in self.backend.calls if c[1:2] == ('play',)], [], 'resume is always explicit')
        self.assertIsNotNone(snapshot['resume_offer'], 'a closed window still leaves the Resume offer')
        active = [(p, v) for p, v in self.video_mentions(snapshot)
                  if str(v.get('status', '')).lower() in ('playing', 'paused', 'watching') or v.get('active') is True]
        self.assertEqual(active, [], 'a closed video window is not reported as playing')

    def test_watch_with_no_room_just_plays(self):
        self.assertIsNone(self.controller.active)
        self.start_video()
        self.settle(2)
        snapshot = self.controller.snapshot()
        self.assertIsNone(snapshot['active'], 'Watch does not invent a room')
        self.assertIsNone(snapshot['resume_offer'], 'nothing to resume without a room')
        self.assertEqual([c for c in self.backend.calls if c[0] in ('music', 'atmosphere', 'picture') or c[0] in EFFECTS], [])
        self.assertIsNotNone(self.processes.player(VIDEO_UNIT))
        self.assertFalse(self.processes.player(VIDEO_UNIT)['pause'])
        self.stop_video()
        self.assertIsNone(self.controller.snapshot()['resume_offer'])

    def test_video_action_is_additive(self):
        if 'video' not in sessions.ACTIONS:
            self.fail('the controller has no `video` action (criterion 5)')
        for name in ('quiet', 'resume', 'stop', 'leave', 'link', 'picture', 'layer'):
            self.assertIn(name, sessions.ACTIONS, name + ' must stay')
        before = dict(self.controller.snapshot().get('errors') or {})
        try:
            self.action('video', {'action': 'rewind-the-world'})
            refused = (self.controller.snapshot().get('errors') or {}) != before
        except sessions.SessionError:
            refused = True
        self.assertTrue(refused, 'an unknown video action is refused (raised or reported as an error)')
        self.assertEqual(self.processes.spawned(), [], 'an unknown video action opens nothing')


class ShowAsSceneOracles(Oracle):
    """Criterion 2: Show as scene replaces only the picture layer and starts no sound."""
    backend_class = RealPictureBackend

    def test_show_as_scene_is_a_silent_picture_and_touches_no_sound(self):
        self.enter_room()
        self.processes.calls.clear()
        before = {k: json.dumps(self.controller.active.get(k), sort_keys=True) for k in ROOM_STATES}
        self.action('link', {'url': VIDEO_URL, 'destination': 'picture'})
        launches = self.processes.spawned()
        self.assertEqual([next(a for a in argv if a.startswith('--unit=')) for argv in launches], ['--unit=' + PICTURE_UNIT],
                         'only the silent picture window starts')
        self.assertIn('--no-audio', launches[0])
        self.assertEqual(self.processes.spawned(VIDEO_UNIT), [], 'the video window (with sound) never starts')
        self.assertEqual([c for c in self.backend.calls if c[0] in ('music', 'atmosphere') or c[0] in EFFECTS[:3]], [],
                         'music and ambience are untouched')
        for layer in ROOM_STATES:
            self.assertEqual(self.backend.state[layer]['status'], 'Playing', layer)
            self.assertEqual(json.dumps(self.controller.active.get(layer), sort_keys=True), before[layer], layer)
        self.assertIn(VIDEO_ID, json.dumps(self.controller.active.get('picture')), 'the room picture is the video')
        self.assertIsNone(self.controller.snapshot()['resume_offer'])


class NoAccountOracles(unittest.TestCase):
    """Criterion 4: personalised recommendations are out of scope; no half-built account path.

    A source-text check, by necessity: "leave no half-built account path" is a property of the code that no
    runtime scenario without an account can observe. It looks only for yt-dlp's account switches and feeds."""

    ACCOUNT = re.compile(r'--cookies|cookies-from-browser|:ytrec|:ytsubs|:ytfav|:ytwatchlater|:ythis|feed/subscriptions')

    def test_no_youtube_account_path_in_the_product(self):
        hits = []
        for path in sorted(SRC.glob('*.py')):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if self.ACCOUNT.search(line):
                    hits.append('%s:%d: %s' % (path.name, number, line.strip()[:120]))
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
