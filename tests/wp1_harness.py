"""Measurement rig for the WP1 acceptance oracles and the cost probe.

Owned by the acceptance author. It is independent of the implementer's tests: it counts what
the OS and the standard library see (a fake `pactl` behind `subprocess.run`, `open`/`os.replace`
calls on the state files, and accepted connections at fake mpv sockets), not what the product
reports about itself. It works against any source tree (`WP1_SRC`, default this checkout), so
the same rig measures the baseline and the change.

Import this module before any product module: it points HOME and every XDG directory at a
private scratch tree first, and the product reads those paths at import time.
"""
import builtins
import collections
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

SRC = Path(os.environ.get('WP1_SRC') or Path(__file__).resolve().parents[1] / 'src')
ROOT = Path(tempfile.mkdtemp(prefix='wp1o'))
for name in ('home', 'state', 'config', 'cache', 'run'):
    (ROOT / name).mkdir(mode=0o700)
os.environ.update(HOME=str(ROOT / 'home'), XDG_STATE_HOME=str(ROOT / 'state'), XDG_CONFIG_HOME=str(ROOT / 'config'),
                  XDG_CACHE_HOME=str(ROOT / 'cache'), XDG_RUNTIME_DIR=str(ROOT / 'run'))
for name in ('WAYLAND_DISPLAY', 'DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE'):
    os.environ.pop(name, None)
sys.path.insert(0, str(SRC))

import listening_hub as hub  # noqa: E402
import audio_devices  # noqa: E402
import room_session  # noqa: E402

STATE_DIR = ROOT / 'state' / 'listening-hub'
CONFIG_DIR = ROOT / 'config' / 'listening-hub'
RUN_DIR = ROOT / 'run' / 'listening-hub'
RUN_DIR.mkdir(mode=0o700, exist_ok=True)
WATCHED = ('state.json', 'audio.json', 'stations.json', 'room-variants.json', 'session.json', 'overlay-audio.json',
           'overlay-source.json', 'overlay-player.json')
RADIO_SOCKET = 'radio-aaaaaaaaaaaa.sock'


class FakeMpv:
    """A tiny mpv JSON-IPC server that counts accepted connections and answered commands."""

    def __init__(self, path, properties):
        self.path = Path(path)
        self.properties = dict(properties)
        self.connections = 0
        self.commands = collections.Counter()
        self.open_connections = 0
        self.events_before_reply = False
        self.reply_delay = {}
        self.close_after = None
        self.rejected = set()
        self._stop = False
        self._sockets = []
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.path.exists():
            self.path.unlink()
        self._server.bind(str(self.path))
        self._server.listen(16)
        self._server.settimeout(.05)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stop:
            try:
                connection, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self._sockets.append(connection)
                self.connections += 1
                self.open_connections += 1
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        answered = 0
        try:
            with connection, connection.makefile('rwb') as stream:
                for line in stream:
                    request = json.loads(line)
                    command = request['command']
                    with self._lock:
                        self.commands[command[1] if len(command) > 1 else command[0]] += 1
                    answered += 1
                    if self.close_after is not None and answered > self.close_after:
                        return
                    delay = self.reply_delay.get(command[1] if len(command) > 1 else command[0])
                    if delay:
                        threading.Event().wait(delay)
                    if self.events_before_reply:
                        stream.write(b'{"event":"property-change","name":"volume","data":1}\n')
                        stream.write(b'{"event":"idle"}\n')
                    prop = command[1] if len(command) > 1 else None
                    if prop in self.rejected or prop not in self.properties:
                        reply = {'error': 'property unavailable', 'request_id': request['request_id']}
                    else:
                        reply = {'error': 'success', 'data': self.properties[prop], 'request_id': request['request_id']}
                    stream.write((json.dumps(reply) + '\n').encode())
                    stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self.open_connections -= 1

    def close(self):
        self._stop = True
        self._thread.join(1)
        self._server.close()
        for connection in list(self._sockets):  # a dying mpv drops every established connection too
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.path.unlink()


def player_properties(volume=50.0, title='Song'):
    return {'idle-active': False, 'pause': False, 'media-title': title, 'volume': volume, 'mute': False,
            'eof-reached': False, 'time-pos': 12.5, 'duration': 200.0, 'seekable': True}


class FakePactl:
    """Behind subprocess.run; `sinks`, `sink_inputs` and `source_outputs` are what PipeWire would report."""

    def __init__(self):
        self.sinks = [{'name': 'sink-a', 'index': 1, 'description': 'Speakers'}]
        self.default = 'sink-a'
        self.sink_inputs = []
        self.source_outputs = []
        self.fail = None
        self.calls = collections.Counter()

    def run(self, args, **kwargs):
        if args[0] != 'pactl':
            raise OSError('unexpected command ' + str(args))
        key = args[-1] if args[1:2] != ['--format=json'] else 'list ' + args[-1]
        self.calls[key] += 1
        if self.fail:
            return subprocess.CompletedProcess(args, 1, '', 'failed')
        if args[-1] == 'sinks':
            return subprocess.CompletedProcess(args, 0, json.dumps(self.sinks), '')
        if args[-1] == 'sink-inputs':
            return subprocess.CompletedProcess(args, 0, json.dumps(self.sink_inputs), '')
        if args[-1] == 'source-outputs':
            return subprocess.CompletedProcess(args, 0, json.dumps(self.source_outputs), '')
        if args[-1] == 'get-default-sink':
            return subprocess.CompletedProcess(args, 0, self.default + '\n', '')
        return subprocess.CompletedProcess(args, 0, '', '')


def call_stream(role='communication', corked=False):
    return {'index': 42, 'corked': corked, 'properties': {'media.role': role, 'application.name': 'video-call'}}


class Meter:
    """Counts file opens, state writes, socket connects and pactl invocations while active."""

    def __init__(self, pactl):
        self.pactl = pactl
        self.counts = collections.Counter()
        self._undo = []

    def _name(self, path):
        try:
            name = os.path.basename(os.fspath(path))
        except TypeError:
            return None
        return name if name in WATCHED else None

    def __enter__(self):
        meter = self
        real_open, real_replace, real_rename = builtins.open, os.replace, os.rename
        real_connect = socket.socket.connect

        def counting_open(file, mode='r', *args, **kwargs):
            name = meter._name(file) if not isinstance(file, int) else None
            if name:
                meter.counts[('write ' if any(c in str(mode) for c in 'wax+') else 'read ') + name] += 1
            return real_open(file, mode, *args, **kwargs)

        def counting_replace(src, dst, *args, **kwargs):
            name = meter._name(dst)
            if name:
                meter.counts['write ' + name] += 1
            return real_replace(src, dst, *args, **kwargs)

        def counting_rename(src, dst, *args, **kwargs):
            name = meter._name(dst)
            if name:
                meter.counts['write ' + name] += 1
            return real_rename(src, dst, *args, **kwargs)

        builtins.open = counting_open
        io.open = counting_open
        os.replace, os.rename = counting_replace, counting_rename
        self._undo = [lambda: setattr(builtins, 'open', real_open), lambda: setattr(io, 'open', real_open),
                      lambda: setattr(os, 'replace', real_replace), lambda: setattr(os, 'rename', real_rename)]
        real_run = subprocess.run
        subprocess.run = self.pactl.run
        self._undo.append(lambda: setattr(subprocess, 'run', real_run))
        return self

    def __exit__(self, *exc):
        for undo in reversed(self._undo):
            undo()


class Rig:
    """A real Controller and Backend over a scratch state directory and fake players."""

    def __init__(self, playing=False, active=False, audio_settings=None, controller_settings=None, backend=None):
        self.clock_value = [1_000_000.0]
        for directory in (STATE_DIR, CONFIG_DIR):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(mode=0o700, parents=True)
        for item in RUN_DIR.iterdir():
            item.unlink()
        self.pactl = FakePactl()
        self.players = {}
        if audio_settings is not None:
            (CONFIG_DIR / 'audio.json').write_text(json.dumps(audio_settings))
        if playing:
            self.players['radio'] = FakeMpv(RUN_DIR / RADIO_SOCKET, player_properties(50.0, 'Song'))
            self.players['atmosphere'] = FakeMpv(RUN_DIR / 'overlay.sock', player_properties(20.0, 'Rain'))
            self.players['picture'] = FakeMpv(RUN_DIR / 'ambience.sock', player_properties(0, 'Scene'))
            hub.save_json(STATE_DIR / 'state.json', {'radio_socket': RADIO_SOCKET, 'radio_name': 'Test FM',
                                                     'radio_source_id': 'station-1', 'radio_url': 'http://localhost/x',
                                                     'radio_audio': {'volume': 50.0, 'muted': False}})
        self.controller = room_session.Controller(backend=backend, directory=STATE_DIR, clock=lambda: self.clock_value[0])
        if controller_settings:
            self.controller.settings.update(controller_settings)
        if active:
            self.controller.active = {'id': 'room', 'music': {'source_id': 'station-1', 'station': {'id': 'station-1'},
                                                              'volume': 50}}
        self.settle_files()

    def settle_files(self, age=3600):
        """Make saved files look old so the product's mtime cache may trust them."""
        for directory in (STATE_DIR, CONFIG_DIR):
            for path in directory.glob('*.json'):
                stamp = path.stat().st_mtime - age
                os.utime(path, (stamp, stamp))

    def tick(self):
        self.clock_value[0] += 1
        self.controller.refresh()
        self.controller.poll_policy()

    def run_seconds(self, seconds, between=None):
        for second in range(seconds):
            if between:
                between(second)
            self.tick()

    def connections(self):
        return {name: player.connections for name, player in self.players.items()}

    def commands(self):
        return {name: sum(player.commands.values()) for name, player in self.players.items()}

    def close(self):
        for player in self.players.values():
            player.close()
        with contextlib.suppress(Exception):
            self.controller.closed = True
            self.controller.worker.shutdown(wait=False)


@contextlib.contextmanager
def rig(**kwargs):
    value = Rig(**kwargs)
    try:
        yield value
    finally:
        value.close()


class RecordingBackend:
    """A backend for policy scenarios: everything the controller does to the players is logged."""

    def __init__(self):
        self.state = {'music': {'status': 'Playing', 'source_id': 'm1', 'name': 'Music', 'volume': 50.0, 'muted': False},
                      'atmosphere': {'status': 'Playing', 'source_id': 'a1', 'volume': 20.0, 'muted': False},
                      'picture': {'status': 'Playing'}}
        self.calls = []

    def layers(self):
        return json.loads(json.dumps(self.state))

    def _control(self, layer, action, value=None):
        self.calls.append((layer, action, value))
        if action == 'volume':
            self.state[layer]['volume'] = value
        elif action == 'pause':
            self.state[layer]['status'] = 'Paused'

    def music_control(self, action, value=None):
        self._control('music', action, value)

    def atmosphere_control(self, action, value=None):
        self._control('atmosphere', action, value)

    def picture_control(self, action, value=None):
        self._control('picture', action, value)

    def extra_video_control(self, action):
        self.calls.append(('extra_video', action, None))

    def media_details(self):
        return {}


_PAUSE = {'preferred_output': 'sink-a', 'pause_on_disconnect': True}
_SINK = {'name': 'sink-a', 'index': 1, 'description': 'Speakers'}
_CALL = call_stream()
# Each step is (sinks, sink_inputs, source_outputs, pactl_fails) for one second of the refresh loop.
POLICY_SCENARIOS = {
    'pause-on-disconnect then reconnect': dict(
        audio=_PAUSE, steps=[([_SINK], [], [], 0)] * 2 + [([], [], [], 0)] * 2 + [([_SINK], [], [], 0)] * 2),
    'pause disabled: no pause when output vanishes': dict(
        audio=dict(_PAUSE, pause_on_disconnect=False), steps=[([_SINK], [], [], 0), ([], [], [], 0), ([], [], [], 0)]),
    'pactl failure never pauses': dict(
        audio=_PAUSE, steps=[([_SINK], [], [], 0), ([], [], [], 1), ([], [], [], 1), ([], [], [], 0)]),
    'duck on call (audio.json) and restore': dict(
        audio={'duck_on_call': True}, steps=[([_SINK], [], [], 0), ([_SINK], [_CALL], [], 0), ([_SINK], [_CALL], [], 0),
                                              ([_SINK], [], [], 0), ([_SINK], [], [], 0)]),
    'duck on call (controller setting only)': dict(
        audio=None, controller={'duck_on_call': True},
        steps=[([_SINK], [], [], 0), ([_SINK], [_CALL], [], 0), ([_SINK], [], [], 0)]),
    'duck on call (audio.json only)': dict(
        audio={'duck_on_call': True}, controller={'duck_on_call': False},
        steps=[([_SINK], [], [], 0), ([_SINK], [_CALL], [], 0), ([_SINK], [], [], 0)]),
    'call seen only as capture stream (phone role)': dict(
        audio={'duck_on_call': True}, steps=[([_SINK], [], [call_stream('phone')], 0), ([_SINK], [], [], 0)]),
    'corked call stream is not a call': dict(
        audio={'duck_on_call': True}, steps=[([_SINK], [call_stream(corked=True)], [], 0)] * 2),
    'music role stream is not a call': dict(
        audio={'duck_on_call': True}, steps=[([_SINK], [call_stream('music')], [], 0)] * 2),
    'no policy enabled: nothing happens even with a call and no output': dict(
        audio=None, steps=[([], [_CALL], [call_stream()], 0)] * 3),
    'call and disconnect together': dict(
        audio=dict(_PAUSE, duck_on_call=True), steps=[([_SINK], [], [], 0), ([], [_CALL], [], 0), ([_SINK], [], [], 0)]),
    'duck on call while paused-by-disconnect already': dict(
        audio=dict(_PAUSE, duck_on_call=True), steps=[([], [], [], 0), ([], [_CALL], [], 0), ([], [], [], 0)]),
}


def run_policy_scenario(spec):
    """Observable behaviour (player commands and policy verdicts) of the controller over the steps."""
    backend = RecordingBackend()
    audio_devices._previous_output = None  # module state would otherwise leak between scenarios
    with rig(audio_settings=spec.get('audio'), controller_settings=spec.get('controller'), backend=backend) as r:
        r.controller.active = {'id': 'room', 'music': {'source_id': 'm1', 'volume': 50},
                               'atmosphere': {'source_id': 'a1', 'volume': 20}}
        log = []
        for sinks, sink_inputs, source_outputs, fails in spec['steps']:
            r.pactl.sinks, r.pactl.sink_inputs, r.pactl.source_outputs, r.pactl.fail = sinks, sink_inputs, source_outputs, bool(fails)
            before = len(backend.calls)
            real_run = subprocess.run
            subprocess.run = r.pactl.run
            try:
                r.tick()
            finally:
                subprocess.run = real_run
            policy = r.controller.policy_status
            log.append({'calls': [list(c) for c in backend.calls[before:]],
                        'disconnect': bool(policy.get('disconnect')), 'reconnected': bool(policy.get('reconnected')),
                        'duck': bool(policy.get('duck')), 'call_active': bool(policy.get('call_active')),
                        'has_error': bool(policy.get('error')),
                        'ducked': sorted((r.controller.ducked or {}).keys()) if r.controller.ducked is not None else None,
                        'resume_offer': bool(r.controller.resume_offer)})
        return log
