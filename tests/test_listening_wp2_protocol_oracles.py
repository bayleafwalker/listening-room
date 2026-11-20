"""Independent acceptance oracles for WP2-PROTOCOL (push updates and one action registry).

Owned by the acceptance author, not the implementer. The oracles talk to the controller the way
clients do: JSON lines over the private Unix socket (`make_server` + `Service` around a real
`Controller` with a fake backend), and `roomctl` run as a subprocess. Players are fake; no unit,
audio, D-Bus or network is touched. They run against any source tree (`WP2_ORACLE_SRC`, default
this checkout), so the same file was run against main (25cda4a) as a negative control.

Where the protocol shape is the implementer's choice the oracles accept any of these forms:

* a subscribe request is `{"action": "subscribe", "args": {}}` on a fresh connection;
* each streamed line is JSON, either a bare snapshot or an envelope `{"ok": true, "result": ...}`
  whose result is the snapshot or holds it under `snapshot`;
* the revision is an integer under `revision` or `rev`, on the snapshot or its envelope;
* lines without a snapshot (acknowledgements, heartbeats) are allowed and do not count as changes;
* an error is a line with `"ok": false`.

Checks marked ADAPT depend on the client API name in `session_client` (see `client_api`) and may be
adapted in place, keeping the original, once the implementer states the signature.

Run: scripts/listening-dev.sh test test_listening_wp2_protocol_oracles.py
"""
import copy
import inspect
import json
import os
from pathlib import Path
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SRC = Path(os.environ.get('WP2_ORACLE_SRC') or Path(os.environ.get(
    'LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent)
sys.path.insert(0, str(SRC))
os.environ.setdefault('LISTENING_HUB_CATALOG', str(SRC / 'catalogue.json'))

import room_session  # noqa: E402
import session_client  # noqa: E402
import session_service  # noqa: E402

MAX_MESSAGE = 1024 * 1024
GENERIC_ACK = {'accepted': True, 'message': 'Updating room'}
# Snapshot keys an existing polling client reads from `inspect` on main (25cda4a).
BASELINE_SNAPSHOT_KEYS = {'active', 'last_session', 'variants', 'layers', 'settings', 'recent', 'resume_offer',
                          'transition', 'errors', 'media', 'audio_policy', 'room_volume', 'last_entered_at', 'now',
                          'timers', 'levels', 'stations', 'station_recent', 'station_learned', 'config_generation',
                          'station'}
# Actions a subscriber, the client API or `roomctl status --follow` may send. Anything else could change
# or start playback.
READ_ONLY = re.compile(r'^(inspect|get|subscribe|unsubscribe|ping|hello|version|heartbeat)$')


def baseline_status(snapshot):
    """roomctl.status on main (25cda4a), frozen: the Waybar line format old setups parse."""
    import html
    active = snapshot.get('active') or {}
    layers = snapshot.get('layers') or {}
    music = layers.get('music') or {}
    atmosphere = layers.get('atmosphere') or {}
    playing = [item.get('name') or item.get('title') for item in (music, atmosphere)
               if item.get('status') == 'Playing' and (item.get('name') or item.get('title'))]
    station = snapshot.get('station') or {}
    name = station.get('name') or (station.get('definition') or {}).get('name') or active.get('name') or 'Listening room'
    caption = name + (' · ' + ' + '.join(playing) if playing else '')
    return {'text': html.escape(caption), 'tooltip': html.escape(caption + '\nClick for room controls; right-click to browse'),
            'class': 'playing' if playing else 'paused' if active else 'stopped', 'alt': 'room'}


def old_exchange(path, action, args=None, timeout=2):
    """A polling client as shipped before WP2: one request line, one response line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps({'action': action, 'args': args or {}}).encode() + b'\n')
        response = bytearray()
        while b'\n' not in response:
            part = connection.recv(65536)
            if not part:
                raise ConnectionError('closed before a response line')
            response.extend(part)
        return json.loads(response.split(b'\n', 1)[0])


# --------------------------------------------------------------------------- protocol shape helpers

def _candidates(obj):
    if not isinstance(obj, dict):
        return []
    found = [obj]
    for key in ('result', 'snapshot', 'data', 'state'):
        inner = obj.get(key)
        if isinstance(inner, dict):
            found.append(inner)
            for key2 in ('snapshot', 'state'):
                if isinstance(inner.get(key2), dict):
                    found.append(inner[key2])
    return found


def snapshot_of(obj):
    for candidate in _candidates(obj):
        if 'layers' in candidate and 'room_volume' in candidate:
            return candidate
    return None


def revision_of(obj):
    for candidate in _candidates(obj):
        for key in ('revision', 'rev'):
            value = candidate.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def is_error(obj):
    return isinstance(obj, dict) and obj.get('ok') is False


def protocol_version(snapshot):
    for value in (snapshot.get('version'), snapshot.get('protocol_version'), (snapshot.get('protocol') or {}).get('version')
                  if isinstance(snapshot.get('protocol'), dict) else None):
        if (isinstance(value, int) and not isinstance(value, bool) and value >= 1) or (isinstance(value, str) and value):
            return value
    return None


class Subscriber:
    """A raw subscribe connection. It records every line with its arrival time."""

    def __init__(self, path, read=True, rcvbuf=None, args=None, payload=None):
        self.lines, self.raw_lengths, self.closed, self.error = [], [], threading.Event(), None
        self.changed = threading.Condition()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(5)
        deadline = time.monotonic() + 3
        while True:  # a full listen backlog answers EAGAIN to a socket with a timeout; a blocking client would wait
            try:
                self.sock.connect(str(path))
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.01)
        self.sock.sendall(payload if payload is not None else
                          json.dumps({'action': 'subscribe', 'args': args or {}}).encode() + b'\n')
        self.thread = None
        if read:
            self.start()

    def start(self):
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        buffer = bytearray()
        try:
            self.sock.settimeout(None)
            while True:
                part = self.sock.recv(262144)
                if not part:
                    break
                buffer.extend(part)
                while b'\n' in buffer:
                    line, _, rest = bytes(buffer).partition(b'\n')
                    buffer = bytearray(rest)
                    self._add(line)
                if len(buffer) > MAX_MESSAGE + 1:
                    self.raw_lengths.append(len(buffer))
                    self.error = 'line longer than MAX_MESSAGE'
                    break
        except OSError as error:
            self.error = str(error)
        finally:
            self.closed.set()
            with self.changed:
                self.changed.notify_all()

    def _add(self, line):
        self.raw_lengths.append(len(line) + 1)
        try:
            obj = json.loads(line)
        except ValueError:
            obj = {'_unparsable': line[:200]}
        with self.changed:
            self.lines.append((time.monotonic(), obj))
            self.changed.notify_all()

    def snapshots(self):
        with self.changed:
            return [(t, snapshot_of(o), revision_of(o)) for t, o in self.lines if snapshot_of(o) is not None]

    def advances(self):
        """Lines whose revision is greater than every earlier revision on this connection."""
        result, best = [], None
        for t, snap, rev in self.snapshots():
            if rev is not None and (best is None or rev > best):
                result.append((t, snap, rev))
                best = rev
        return result

    def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                for t, obj in self.lines:
                    if predicate(obj):
                        return t, obj
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed.is_set():
                    return None, None
                self.changed.wait(remaining)

    def wait_snapshot(self, predicate=lambda s: True, timeout=2):
        t, obj = self.wait(lambda o: snapshot_of(o) is not None and predicate(snapshot_of(o)), timeout)
        return t, (snapshot_of(obj) if obj is not None else None)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- fake backend and rig

class FakeBackend:
    def __init__(self):
        self.state = {name: {'status': 'Stopped'} for name in ('music', 'atmosphere', 'picture')}
        self.calls = []
        self.block = None
        self.media = {}

    def layers(self):
        return copy.deepcopy(self.state)

    def start_music(self, source, cancel):
        self.calls.append(('start_music_called', source.get('id')))
        if self.block:
            self.block.wait(3)
        if cancel.is_set():
            raise ValueError('Cancelled before source commit')
        self.calls.append(('start_music', source['id']))
        self.state['music'] = {'status': 'Playing', 'source_id': source['id'], 'name': source['name'], 'volume': 50, 'muted': False}
        return source

    def start_verified_music(self, source, cancel, audio_file, player_unit=None):
        return self.start_music(source, cancel)

    def media_details(self):
        return copy.deepcopy(self.media)

    def music_control(self, action, value=None):
        self.calls.append(('music', action, value))
        self._control('music', action, value)

    def start_atmosphere(self, selection, cancel):
        if cancel.is_set():
            raise ValueError('Cancelled')
        self.calls.append(('start_atmosphere', copy.deepcopy(selection)))
        self.state['atmosphere'] = dict(selection, status='Stopped' if selection.get('kind') == 'none' else 'Playing')

    def atmosphere_control(self, action, value=None):
        self.calls.append(('atmosphere', action, value))
        self._control('atmosphere', action, value)

    def start_picture(self, picture, cancel=None):
        if cancel is not None and cancel.is_set():
            raise ValueError('Picture start cancelled')
        self.calls.append(('start_picture', copy.deepcopy(picture)))
        self.state['picture'] = dict(picture, status='Playing' if picture.get('enabled') else 'Stopped')

    def picture_control(self, action, value=None):
        self.calls.append(('picture', action, value))
        self._control('picture', action, value)

    def extra_video_control(self, action):
        self.calls.append(('owned_video', action))

    def _control(self, layer, action, value):
        if action in ('pause', 'play', 'stop'):
            self.state[layer]['status'] = {'pause': 'Paused', 'play': 'Playing', 'stop': 'Stopped'}[action]
        elif action in ('volume', 'muted'):
            self.state[layer][action] = value


SOURCES = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'},
           {'id': 'ambient', 'name': 'Ambient', 'kind': 'radio', 'url': 'https://example.test/ambient'}]
ROOMS = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'},
         {'id': 'north', 'name': 'Northern quiet', 'source_id': 'ambient', 'view_id': 'room-north'}]


class Rig:
    """A real Controller with a fake backend behind the real socket server."""

    def __init__(self, test, path=None, directory=None):
        self.test = test
        self.directory = directory or test.state_dir
        self.path = Path(path or test.socket_path)
        self.backend = FakeBackend()
        self.controller = room_session.Controller(self.backend, self.directory)
        self.patches = [mock.patch.object(room_session.hub, 'sources', return_value=SOURCES),
                        mock.patch.object(room_session.room_catalogue, 'rooms', return_value=ROOMS),
                        mock.patch.object(self.controller, '_home_action'),
                        mock.patch.object(self.controller, '_route_preferred')]
        for patch in self.patches:
            patch.start()
        self.actions = []
        dispatch = self.controller.dispatch

        def logged_dispatch(action, args=None, *rest, **kwargs):
            self.actions.append(('dispatch', action))
            return dispatch(action, args, *rest, **kwargs)
        self.controller.dispatch = logged_dispatch
        self.service = session_service.Service(self.controller)
        execute = self.service.execute

        def logged_execute(action, args, *rest, **kwargs):
            self.actions.append(('execute', action))
            return execute(action, args, *rest, **kwargs)
        self.service.execute = logged_execute
        self.server, self.fd = session_service.make_server(self.path, self.service)
        # REVISED (phase 2): track accepted connections so stop() can end them the way a controller
        # process exit does. Reason: shutdown()+server_close() close only the listening socket; in-process
        # handler threads kept old subscribe streams open, so a "restart" never reached clients (the client
        # waits up to three 15 s heartbeats), unlike the real service, whose exit closes every connection.
        self.accepted = []
        get_request = self.server.get_request

        def tracked_get_request():
            connection, address = get_request()
            self.accepted.append(connection)
            return connection, address
        self.server.get_request = tracked_get_request
        assert self.server is not None, 'socket lock already held'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.stopped = False
        test.addCleanup(self.stop)

    def action_names(self):
        return {name for _, name in self.actions}

    def request(self, action, args=None, timeout=3):
        return old_exchange(self.path, action, args, timeout)

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.server.shutdown()
        for connection in self.accepted:  # REVISED (phase 2), see __init__; ORIGINAL: no such loop
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.server_close()
        try:
            self.controller.close()
        except Exception:
            pass
        for patch in reversed(self.patches):
            patch.stop()
        self.path.unlink(missing_ok=True)
        os.close(self.fd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wp2o')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name in ('home', 'config', 'state', 'cache', 'run', 'session'):
            (root / name).mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(root / 'home'), 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state'),
            'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_RUNTIME_DIR': str(root / 'run')})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('WAYLAND_DISPLAY', 'DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE'):
            if name in os.environ:
                patch = mock.patch.dict(os.environ)
                patch.start()
                self.addCleanup(patch.stop)
                os.environ.pop(name, None)
        self.state_dir = root / 'session'
        self.socket_dir = root / 'run' / 'listening-hub'
        self.socket_path = self.socket_dir / 'session.sock'

    def rig(self, **kwargs):
        return Rig(self, **kwargs)

    def subscribe(self, rig, **kwargs):
        subscriber = Subscriber(rig.path, **kwargs)
        self.addCleanup(subscriber.close)
        return subscriber

    def first_snapshot(self, subscriber, timeout=2):
        t, snap = subscriber.wait_snapshot(timeout=timeout)
        self.assertIsNotNone(snap, 'subscribe sent no snapshot within %ss; lines: %r' % (timeout, subscriber.lines[:3]))
        return snap

    def roomctl(self, *arguments, env=None):
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), os.environ.get('PYTHONPATH', '')]),
                           LISTENING_SESSION_SOCKET=str(self.socket_path), PYTHONUNBUFFERED='0')
        environment.pop('PYTHONUNBUFFERED')
        environment.update(env or {})
        return subprocess.Popen([sys.executable, str(SRC / 'roomctl.py'), *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=environment)


class LineReader:
    def __init__(self, stream):
        self.lines, self.cond = [], threading.Condition()
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream):
        for raw in iter(stream.readline, b''):
            with self.cond:
                self.lines.append((time.monotonic(), raw.decode(errors='replace').rstrip('\n')))
                self.cond.notify_all()

    def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for t, line in self.lines:
                    if predicate(line):
                        return t, line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, None
                self.cond.wait(remaining)


def waybar(line):
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) and {'text', 'tooltip', 'class'} <= set(value) else None


# =========================================================================== criterion 1: registry

class RegistryOracles(Base):
    def test_protocol_version_is_reported_by_inspect_and_get(self):
        rig = self.rig()
        for action in ('inspect', 'get'):
            response = rig.request(action)
            self.assertTrue(response['ok'], response)
            self.assertIsNotNone(protocol_version(response['result']),
                                 '%s reports no protocol version (version, protocol_version or protocol.version)' % action)
        self.assertEqual(protocol_version(rig.request('inspect')['result']), protocol_version(rig.request('get')['result']))

    def test_dispatch_is_table_driven_not_a_chain_of_action_branches(self):
        """Structural: main has about 60 `action ==` branches in Controller.dispatch."""
        source = inspect.getsource(room_session.Controller.dispatch)
        branches = len(re.findall(r"\baction\s*(==|in\s*\(|\.startswith\()", source))
        self.assertLessEqual(branches, 6, 'Controller.dispatch still branches on the action name %d times' % branches)

    def test_service_has_no_separate_hard_coded_immediate_action_list(self):
        """Structural: the immediate list in session_service comes from the registry."""
        source = inspect.getsource(session_service.Service)
        names = [n for n in ('wind_down', 'room_volume', 'rename', 'variants', 'resume', 'online', 'settings')
                 if re.search(r"['\"]%s['\"]" % n, source)]
        self.assertEqual(names, [], 'Service still names actions literally: %s' % names)

    def immediate_cases(self):
        item = dict(SOURCES[0])
        return [('inspect', {}), ('get', {}), ('enter', {'room_id': 'library'}), ('music', {'item': item}),
                ('online', {'item': 'not-an-object'}), ('return', {}), ('quiet', {}), ('resume', {}), ('stop', {}),
                ('leave', {'fade_seconds': 0}), ('wind_down', {'minutes': 0}), ('variants', {}), ('delete', {'id': 'missing'}),
                ('rename', {'id': 'missing', 'name': 'X'}), ('settings', {'values': {}}), ('room_volume', {'value': 40}),
                ('layer', {'layer': 'music', 'action': 'pause'}), ('layer', {'layer': 'music', 'action': 'stop'}),
                ('picture', {'action': 'stop'}), ('picture', {'action': 'pause'}),
                ('station.list', {}), ('station.recent', {}), ('metadata.jobs', {}), ('metadata.status', {})]

    def queued_cases(self):
        return [('save', {'name': 'Queued save'}), ('layer', {'layer': 'music', 'action': 'volume', 'value': 10}),
                ('layer', {'layer': 'music', 'action': 'mute'}), ('picture', {'action': 'select', 'id': 'none'}),
                ('atmosphere', {'source_id': 'none'}), ('link', {'url': 'https://example.test/x'})]

    def test_immediate_actions_still_answer_while_the_worker_is_busy(self):
        """main's immediate list: each returns its own result (or error) without waiting for queued work."""
        rig = self.rig()
        release = threading.Event()
        rig.controller.worker.submit(release.wait, 5)
        self.addCleanup(release.set)
        slow = []
        for action, args in self.immediate_cases():
            started = time.monotonic()
            try:
                result = rig.service.execute(action, copy.deepcopy(args))
            except Exception as error:  # a synchronous error is an immediate answer too
                result = {'error': str(error)}
            elapsed = time.monotonic() - started
            if elapsed > .5 or result == GENERIC_ACK:
                slow.append((action, args.get('action'), round(elapsed, 3), result))
        release.set()
        self.assertEqual(slow, [], 'these immediate actions were queued or waited for the worker')

    def test_queued_actions_are_still_queued_behind_accepted_work(self):
        rig = self.rig()
        rig.service.execute('enter', {'room_id': 'library'})
        rig.controller.worker.submit(lambda: None).result(3)
        rig.controller.refresh()
        release = threading.Event()
        rig.controller.worker.submit(release.wait, 5)
        self.addCleanup(release.set)
        before = list(rig.backend.calls)
        variants = rig.controller.variants()
        wrong = []
        for action, args in self.queued_cases():
            started = time.monotonic()
            result = rig.service.execute(action, copy.deepcopy(args))
            if time.monotonic() - started > .5 or not (isinstance(result, dict) and result.get('accepted') is True
                                                      and result.get('message') == 'Updating room'):
                wrong.append((action, args, result))
        self.assertEqual(wrong, [], 'these queued actions no longer return the queued acknowledgement')
        self.assertEqual(rig.backend.calls, before, 'a queued action ran before the accepted work finished')
        self.assertEqual(rig.controller.variants(), variants)
        release.set()
        rig.controller.worker.submit(lambda: None).result(3)
        self.assertIn(('music', 'volume', rig.controller._effective(10)), rig.backend.calls)
        self.assertEqual(rig.controller.variants()[0]['name'], 'Queued save')

    def test_unknown_action_fails_clearly_and_disturbs_nothing(self):
        rig = self.rig()
        rig.request('enter', {'room_id': 'library'})
        rig.controller.worker.submit(lambda: None).result(3)
        generation, calls = rig.controller.generation, list(rig.backend.calls)
        response = rig.request('no_such_action', {'x': 1})
        if response['ok']:  # today: queued, then reported in errors
            deadline = time.monotonic() + 1
            message = ''
            while time.monotonic() < deadline and 'no_such_action' not in message:
                message = json.dumps(rig.request('inspect')['result']['errors'])
                time.sleep(.02)
            self.assertIn('no_such_action', message)
        else:
            self.assertRegex(response['error'], r'no_such_action|[Uu]nknown')
            self.assertNotIn('Traceback', response['error'])
        self.assertEqual(rig.controller.generation, generation, 'an unknown action invalidated in-flight work')
        self.assertEqual(rig.backend.calls, calls)
        self.assertIsNotNone(rig.controller.active)

    def test_malformed_requests_and_arguments_fail_with_a_short_clear_error_and_change_nothing(self):
        rig = self.rig()
        rig.request('room_volume', {'value': 70})
        stored = (self.state_dir / 'session.json').read_bytes()
        raw_cases = [b'{"action":"inspect","args":[1]}\n', b'{"action":7}\n', b'[1,2]\n', b'not json\n',
                     b'{"action":"room_volume","args":{"value":NaN}}\n', b'{"action":"room_volume","args":"x"}\n']
        for payload in raw_cases:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(str(rig.path))
                connection.sendall(payload)
                response = json.loads(connection.makefile('rb').readline())
            self.assertFalse(response['ok'], payload)
            self.assertTrue(response['error'] and len(response['error']) < 300, response)
        cases = [('room_volume', {'value': 'loud'}), ('room_volume', {'value': 101}), ('room_volume', {'delta': True}),
                 ('room_volume', {}), ('wind_down', {'minutes': 5000}), ('wind_down', {'minutes': 'abc'}),
                 ('online', {'item': 'x'}), ('enter', {'room_id': 'no-such-room'})]
        for action, args in cases:
            response = rig.request(action, args)
            if response['ok']:
                self.fail('%s %r was accepted: %r' % (action, args, response))
            self.assertTrue(response['error'] and len(response['error']) < 300 and 'Traceback' not in response['error'],
                            (action, args, response))
        self.assertEqual(rig.controller.room_volume, 70)
        self.assertEqual((self.state_dir / 'session.json').read_bytes(), stored)

    def test_representative_calls_from_existing_clients_keep_working(self):
        """Argument shapes the window, mini player, roomctl and MPRIS send today; extra keys stay tolerated."""
        rig = self.rig()
        calls = [('inspect', {}), ('enter', {'room_id': 'library'}), ('room_volume', {'value': 60}),
                 ('room_volume', {'delta': -5}), ('quiet', {'minutes': 10.0}), ('resume', {}),
                 ('layer', {'layer': 'music', 'action': 'pause'}), ('settings', {'values': {}}),
                 ('station.list', {}), ('metadata.jobs', {}), ('variants', {}),
                 ('room_volume', {'value': 50, 'client': 'old-widget'}), ('stop', {})]
        for action, args in calls:
            response = rig.request(action, args)
            self.assertTrue(response['ok'], (action, args, response))
            rig.controller.worker.submit(lambda: None).result(3)
        self.assertEqual(rig.controller.room_volume, 50)


# =========================================================================== criterion 2: subscribe

class SubscribeOracles(Base):
    def test_subscribe_sends_the_current_revisioned_snapshot_promptly(self):
        rig = self.rig()
        rig.request('room_volume', {'value': 64})
        inspected = rig.request('inspect')['result']
        subscriber = self.subscribe(rig)
        snap = self.first_snapshot(subscriber, 1)
        self.assertEqual(snap['room_volume'], 64)
        self.assertLessEqual(BASELINE_SNAPSHOT_KEYS, set(snap), 'streamed snapshot lacks inspect keys')
        # ORIGINAL (phase 1): self.assertLessEqual(set(inspected) - {'now'}, set(snap) | {'revision', 'rev'})
        # REVISED (phase 2): the protocol version is connection metadata, not room state; the stream carries
        # it in its envelope, not inside each snapshot. Every state key must still be streamed.
        protocol_keys = {'revision', 'rev', 'version', 'protocol_version', 'protocol'}
        self.assertLessEqual(set(inspected) - {'now'} - protocol_keys, set(snap))
        self.assertIsNotNone(subscriber.snapshots()[0][2], 'streamed snapshot carries no integer revision')

    def test_each_change_streams_one_line_with_a_higher_revision(self):
        rig = self.rig()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        for value in (11, 22, 33):
            before = len(subscriber.advances())
            rig.request('room_volume', {'value': value})
            t, snap = subscriber.wait_snapshot(lambda s, v=value: s['room_volume'] == v, 1)
            self.assertIsNotNone(snap, 'change to %s never streamed' % value)
            time.sleep(.15)
            new = subscriber.advances()[before:]
            self.assertTrue(1 <= len(new) <= 2, 'one room_volume change produced %d revisions' % len(new))
        revisions = [rev for _, _, rev in subscriber.snapshots() if rev is not None]
        self.assertEqual(revisions, sorted(revisions), 'revisions went backwards on one connection')
        lines = [json.dumps(obj) for _, obj in subscriber.lines]
        self.assertTrue(all('_unparsable' not in line for line in lines))

    def test_external_player_change_seen_on_refresh_streams_promptly(self):
        rig = self.rig()
        rig.controller.refresh()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        rig.backend.state['music'] = {'status': 'Playing', 'name': 'Outside player', 'source_id': 'x', 'volume': 30}
        started = time.monotonic()
        rig.controller.refresh()
        t, snap = subscriber.wait_snapshot(lambda s: (s['layers'].get('music') or {}).get('status') == 'Playing', 1)
        self.assertIsNotNone(snap)
        self.assertLess(t - started, .1)

    def test_unchanged_refreshes_do_not_stream_new_revisions(self):
        """The service refreshes every second; an idle room must not turn that back into polling."""
        rig = self.rig()
        rig.controller.refresh()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        time.sleep(.2)
        before = len(subscriber.advances())
        for _ in range(5):
            rig.controller.refresh()
            time.sleep(.1)
        time.sleep(.2)
        self.assertEqual(len(subscriber.advances()), before, 'unchanged refreshes streamed new revisions')

    def test_idle_subscriber_survives_past_the_request_timeout_and_heartbeats_are_sparse(self):
        rig = self.rig()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        count = len(subscriber.lines)
        time.sleep(4.5)
        self.assertFalse(subscriber.closed.is_set(), 'idle subscriber was disconnected: %s' % subscriber.error)
        self.assertLessEqual(len(subscriber.lines) - count, 5, 'more than one heartbeat line a second while idle')
        rig.request('room_volume', {'value': 12})
        self.assertIsNotNone(subscriber.wait_snapshot(lambda s: s['room_volume'] == 12, 1)[1])

    def test_subscribers_on_separate_connections_do_not_share_or_lose_updates(self):
        rig = self.rig()
        first, second = self.subscribe(rig), self.subscribe(rig)
        self.first_snapshot(first), self.first_snapshot(second)
        second.close()
        rig.request('room_volume', {'value': 81})
        self.assertIsNotNone(first.wait_snapshot(lambda s: s['room_volume'] == 81, 1)[1])
        third = self.subscribe(rig)
        self.assertEqual(self.first_snapshot(third)['room_volume'], 81)


# =========================================================================== criterion 2: client API (ADAPT)

def client_api():
    """ADAPT: locate the subscribe client API in session_client.

    Accepts a generator/iterator function named subscribe/watch/follow/updates/snapshots, or one taking a
    callback. Returns a callable run(path, on_snapshot, stop_event) that runs until stop_event is set.
    """
    for name in ('subscribe', 'watch', 'follow', 'updates', 'snapshots', 'stream'):
        candidate = getattr(session_client, name, None)
        if callable(candidate):
            break
    else:
        return None
    parameters = inspect.signature(candidate).parameters

    def run(path, on_snapshot, stop):
        kwargs = {}
        if 'path' in parameters:
            kwargs['path'] = path
        if 'stop' in parameters:
            kwargs['stop'] = stop
        elif 'stop_event' in parameters:
            kwargs['stop_event'] = stop
        callback_name = next((p for p in parameters if p in ('callback', 'on_snapshot', 'on_update', 'handler')), None)
        if callback_name:
            # ORIGINAL (phase 1): kwargs[callback_name] = lambda snap, *_: on_snapshot(snap)
            # REVISED (phase 2, ADAPT): the callback receives an Update(snapshot, revision, session, connected,
            # error) namedtuple; hand on its snapshot (connected=False updates carry None and are skipped).
            kwargs[callback_name] = lambda update, *_: (on_snapshot(getattr(update, 'snapshot', update))
                                                        if getattr(update, 'snapshot', update) is not None else None)
            result = candidate(**kwargs)
            if hasattr(result, 'close') and not inspect.isgenerator(result):
                stop_waiter = threading.Thread(target=lambda: (stop.wait(), result.close()), daemon=True)
                stop_waiter.start()
                stop.wait()
            elif inspect.isgenerator(result) or hasattr(result, '__next__'):
                for _ in result:
                    if stop.is_set():
                        break
            else:
                stop.wait()
            return
        for snap in candidate(**kwargs):
            on_snapshot(snapshot_of(snap) or snap)
            if stop.is_set():
                break
    return run


class ClientApiOracles(Base):
    def setUp(self):
        super().setUp()
        self.api = client_api()
        self.assertIsNotNone(self.api, 'ADAPT: no subscribe client API found in session_client')
        self.seen, self.cond, self.stop_event = [], threading.Condition(), threading.Event()
        self.addCleanup(self.stop_event.set)
        environment = mock.patch.dict(os.environ, {'LISTENING_SESSION_SOCKET': str(self.socket_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def on_snapshot(self, snap):
        with self.cond:
            self.seen.append((time.monotonic(), snapshot_of(snap) or snap))
            self.cond.notify_all()

    def start_client(self):
        thread = threading.Thread(target=self.api, args=(self.socket_path, self.on_snapshot, self.stop_event), daemon=True)
        thread.start()
        return thread

    def wait_seen(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for t, snap in self.seen:
                    if isinstance(snap, dict) and predicate(snap):
                        return t
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def test_client_receives_changes_and_reconnects_after_a_controller_restart_without_starting_playback(self):
        rig = self.rig()
        with mock.patch.object(session_client.subprocess, 'Popen') as popen, \
                mock.patch.object(session_client.subprocess, 'run') as run:
            self.start_client()
            self.assertIsNotNone(self.wait_seen(lambda s: 'room_volume' in s, 2), 'client delivered no snapshot')
            started = time.monotonic()
            rig.request('room_volume', {'value': 37})
            t = self.wait_seen(lambda s: s.get('room_volume') == 37, 1)
            self.assertIsNotNone(t)
            self.assertLess(t - started, .1)
            rig.stop()
            time.sleep(1.5)
            restarted = self.rig()
            restarted.backend.state['music'] = {'status': 'Playing', 'name': 'After restart', 'source_id': 'x', 'volume': 20}
            restarted.controller.refresh()
            t = self.wait_seen(lambda s: (s.get('layers', {}).get('music') or {}).get('name') == 'After restart', 6)
            self.assertIsNotNone(t, 'client did not reconnect to the restarted controller (or ignored its lower revision)')
            popen.assert_not_called()
            run.assert_not_called()
        # ORIGINAL (phase 1):
        # mutating = {name for name in rig.action_names() | restarted.action_names() if not READ_ONLY.match(name)}
        # self.assertEqual(mutating, set(), 'the client API sent actions that could start playback')
        # REVISED (phase 2): the log also holds the test's own room_volume request (an oracle bug, not the
        # client's). Exclude exactly that one request and require it was the only room_volume.
        mutating = {name for name in rig.action_names() | restarted.action_names() if not READ_ONLY.match(name)}
        self.assertEqual(mutating - {'room_volume'}, set(), 'the client API sent actions that could start playback')
        self.assertEqual([a for a in rig.actions + restarted.actions if a == ('execute', 'room_volume')],
                         [('execute', 'room_volume')], 'the client API sent room_volume')
        self.assertIsNone(restarted.controller.active)
        self.assertFalse([c for c in restarted.backend.calls if c[0].startswith('start')])

    def test_client_backs_off_against_a_controller_that_accepts_and_drops(self):
        self.socket_dir.mkdir(mode=0o700, exist_ok=True)
        accepts = []
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(64)
        listener.settimeout(.1)
        self.addCleanup(listener.close)
        self.start_client()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            accepts.append(time.monotonic())
            connection.close()
        self.stop_event.set()
        self.assertGreaterEqual(len(accepts), 1, 'client never tried to connect')
        self.assertLessEqual(len(accepts), 20, 'client reconnected %d times in 2.5 s: no backoff' % len(accepts))


# =========================================================================== criterion 3: roomctl and old clients

class RoomctlOracles(Base):
    def follow(self, env=None):
        process = self.roomctl('status', '--follow', env=env)
        self.addCleanup(lambda: (process.kill(), process.wait(5), process.stdout.close(), process.stderr.close()))
        return process, LineReader(process.stdout)

    def test_status_without_follow_is_unchanged_for_running_and_absent_controllers(self):
        process = self.roomctl('status')
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(json.loads(out.decode().strip()), baseline_status({}))
        rig = self.rig()
        rig.backend.state['music'] = {'status': 'Playing', 'name': 'Probe radio', 'source_id': 'x', 'volume': 30}
        rig.controller.refresh()
        process = self.roomctl('status')
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out.decode().count('\n'), 1)
        self.assertEqual(json.loads(out), baseline_status(rig.controller.snapshot()))
        self.assertEqual(rig.action_names() - {'inspect', 'get'}, set())

    def test_follow_prints_waybar_lines_on_change_without_polling(self):
        rig = self.rig()
        rig.controller.refresh()
        process, reader = self.follow()
        t, line = reader.wait(lambda l: waybar(l) is not None, 5)
        if line is None:
            alive = process.poll() is None
            self.fail('no initial Waybar line (stdout not flushed?); exited=%s stderr=%r'
                      % (not alive, b'' if alive else process.stderr.read(400)))
        self.assertEqual(waybar(line), baseline_status(rig.controller.snapshot()))
        rig.backend.state['music'] = {'status': 'Playing', 'name': 'Probe radio', 'source_id': 'x', 'volume': 30}
        started = time.monotonic()
        rig.controller.refresh()
        t, line = reader.wait(lambda l: (waybar(l) or {}).get('class') == 'playing', 2)
        self.assertIsNotNone(line, 'follow did not print the change')
        self.assertLess(t - started, .5)
        self.assertEqual(waybar(line), baseline_status(rig.controller.snapshot()))
        inspects_before = sum(1 for _, name in rig.actions if name in ('inspect', 'get'))
        time.sleep(2.5)
        inspects = sum(1 for _, name in rig.actions if name in ('inspect', 'get')) - inspects_before
        self.assertLessEqual(inspects, 1, 'follow polls inspect (%d calls in 2.5 s)' % inspects)
        self.assertIsNone(process.poll(), 'follow exited')
        self.assertEqual({n for n in rig.action_names() if not READ_ONLY.match(n)}, set())

    def test_follow_survives_a_controller_restart_and_never_starts_the_controller_or_playback(self):
        """Interpretation: --follow keeps status's start=False; while the controller is away it shows the
        stopped line and waits, and it never asks systemd for the service or spawns one."""
        fake_bin = Path(self.tmp.name) / 'bin'
        fake_bin.mkdir()
        log = Path(self.tmp.name) / 'systemctl.log'
        (fake_bin / 'systemctl').write_text('#!/bin/sh\necho "$@" >> %s\nexit 1\n' % log)
        (fake_bin / 'systemctl').chmod(0o755)
        env = {'PATH': str(fake_bin) + os.pathsep + os.environ.get('PATH', ''), 'DISPLAY': ':wp2-oracle-none'}
        rig = self.rig()
        process, reader = self.follow(env)
        self.assertIsNotNone(reader.wait(lambda l: waybar(l) is not None, 5)[1])
        rig.stop()
        self.socket_dir.chmod(0o750)  # any controller spawned now refuses to bind
        time.sleep(2)
        self.assertIsNone(process.poll(), 'follow exited when the controller went away')
        self.assertFalse(log.exists(), 'follow asked systemd to start the controller: %r' % (log.read_text() if log.exists() else ''))
        self.assertFalse(self.socket_path.exists(), 'something bound the controller socket while it was away')
        self.socket_dir.chmod(0o700)
        restarted = self.rig()
        restarted.backend.state['music'] = {'status': 'Playing', 'name': 'After restart', 'source_id': 'x', 'volume': 20}
        restarted.controller.refresh()
        t, line = reader.wait(lambda l: 'After restart' in ((waybar(l) or {}).get('text') or ''), 8)
        self.assertIsNotNone(line, 'follow did not pick up the restarted controller')
        self.assertIsNone(restarted.controller.active)
        self.assertFalse([c for c in restarted.backend.calls if c[0].startswith('start')])
        self.assertEqual({n for n in rig.action_names() | restarted.action_names() if not READ_ONLY.match(n)}, set())

    def test_follow_without_a_controller_prints_the_stopped_line_and_waits(self):
        """Interpretation: Waybar shows what `status` shows today when no controller runs."""
        process, reader = self.follow()
        t, line = reader.wait(lambda l: waybar(l) is not None, 5)
        self.assertEqual(waybar(line) if line else None, baseline_status({}))
        time.sleep(1)
        self.assertIsNone(process.poll(), 'follow exited without a controller')

    def test_follow_backs_off_against_a_controller_that_accepts_and_drops(self):
        self.socket_dir.mkdir(mode=0o700, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(64)
        listener.settimeout(.1)
        self.addCleanup(listener.close)
        process, reader = self.follow()
        accepts, deadline = [], time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            accepts.append(time.monotonic())
            connection.close()
        self.assertIsNone(process.poll(), 'follow exited')
        self.assertGreaterEqual(len(accepts), 1, 'follow never connected')
        late = [t for t in accepts if t > accepts[0] + 1.5]
        self.assertLessEqual(len(late), 12, 'follow reconnected %d times in 2.5 s: no backoff' % len(late))
        self.assertIsNone(process.poll())

    def test_readme_waybar_snippet_uses_follow_without_interval(self):
        readme = (SRC.parent / 'README.md')
        if not readme.exists():
            self.skipTest('README not shipped with this source tree')
        text = readme.read_text()
        block = re.search(r'"custom/listening"\s*=\s*\{(.*?)\n\s*\};', text, re.S)
        self.assertIsNotNone(block, 'README Waybar snippet not found')
        self.assertRegex(block.group(1), r'exec\s*=\s*"[^"]*listening-roomctl status --follow"')
        self.assertNotRegex(block.group(1), r'\binterval\s*=')

    def test_runbook_documents_subscribe_and_the_client_api(self):
        runbook = SRC.parent / 'docs/runbooks/listening-room-session.md'
        if not runbook.exists():
            self.skipTest('runbook not shipped with this source tree')
        text = runbook.read_text()
        self.assertIn('subscribe', text)
        self.assertRegex(text, r'session_client')
        self.assertRegex(text, r'heartbeat', 'runbook does not state the heartbeat interval')
        self.assertRegex(text, r'--follow')


# =========================================================================== criterion 4: latency and cost

class LatencyAndCostOracles(Base):
    def test_change_reaches_a_subscriber_in_under_100_ms(self):
        rig = self.rig()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        latencies = []
        for value in range(20, 40):
            started = time.monotonic()
            rig.request('room_volume', {'value': value})
            t, snap = subscriber.wait_snapshot(lambda s, v=value: s['room_volume'] == v, 1)
            self.assertIsNotNone(snap, 'value %s never arrived' % value)
            latencies.append(t - started)
        under = sum(1 for latency in latencies if latency < .1)
        self.assertGreaterEqual(under, 19, 'latencies: %s' % [round(x * 1000) for x in latencies])
        self.assertLess(max(latencies), .5)
        print('subscribe latency ms: median %.1f max %.1f' % (statistics.median(latencies) * 1000, max(latencies) * 1000),
              file=sys.stderr)

    def test_idle_subscribers_cost_no_cpu(self):
        rig = self.rig()
        subscribers = [self.subscribe(rig) for _ in range(32)]
        for subscriber in subscribers:
            self.first_snapshot(subscriber)
        time.sleep(.3)
        cpu, wall = time.process_time(), time.monotonic()
        time.sleep(3)
        used = time.process_time() - cpu
        self.assertLess(used, .25, '32 idle subscribers used %.3f s CPU in %.1f s' % (used, time.monotonic() - wall))

    def test_many_subscribers_all_get_the_change_and_never_hang(self):
        rig = self.rig()
        subscribers = [self.subscribe(rig) for _ in range(64)]
        served, refused, hung = [], [], []
        for subscriber in subscribers:
            t, obj = subscriber.wait(lambda o: snapshot_of(o) is not None or is_error(o), 2)
            (served if obj is not None and snapshot_of(obj) is not None else refused if obj is not None else hung).append(subscriber)
        self.assertEqual(hung, [], '%d subscribers got neither a snapshot nor an error' % len(hung))
        self.assertGreaterEqual(len(served), 16)
        started = time.monotonic()
        response = old_exchange(rig.path, 'inspect')
        self.assertTrue(response['ok'])
        self.assertLess(time.monotonic() - started, .3, 'inspect slowed by open subscribers')
        started = time.monotonic()
        rig.request('room_volume', {'value': 58})
        self.assertLess(time.monotonic() - started, .3)
        late = [s for s in served if s.wait_snapshot(lambda x: x['room_volume'] == 58, .5)[1] is None]
        self.assertEqual(late, [], '%d of %d subscribers missed the change for 500 ms' % (len(late), len(served)))

    def test_a_subscriber_that_never_reads_blocks_neither_the_controller_nor_others(self):
        rig = self.rig()
        with rig.controller.lock:
            rig.controller.errors['oracle_padding'] = 'x' * 65536  # large snapshots fill socket buffers fast
        stuck = self.subscribe(rig, read=False, rcvbuf=4096)
        fast = self.subscribe(rig)
        self.first_snapshot(fast)
        round_trips, snapshot_calls = [], []
        for value in range(0, 101):
            started = time.monotonic()
            rig.request('room_volume', {'value': value % 100})
            round_trips.append(time.monotonic() - started)
            started = time.monotonic()
            rig.controller.snapshot()
            snapshot_calls.append(time.monotonic() - started)
        self.assertLess(max(round_trips), .25, 'an action waited %.3f s behind a stuck subscriber' % max(round_trips))
        self.assertLess(max(snapshot_calls), .1)
        rig.request('room_volume', {'value': 77})
        self.assertIsNotNone(fast.wait_snapshot(lambda s: s['room_volume'] == 77, .5)[1], 'fast subscriber starved')
        late = self.subscribe(rig)
        self.assertEqual(self.first_snapshot(late, 1)['room_volume'], 77)
        stuck.start()  # drain: it is either closed or catches up; it never sees garbage
        stuck.wait_snapshot(lambda s: s['room_volume'] == 77, 3)
        caught_up = any(s['room_volume'] == 77 for _, s, _ in stuck.snapshots())
        self.assertTrue(caught_up or stuck.closed.is_set(), 'slow subscriber neither caught up nor was dropped')
        self.assertFalse(any('_unparsable' in obj for _, obj in stuck.lines))

    def test_subscribers_that_vanish_mid_stream_leak_nothing_and_block_nothing(self):
        rig = self.rig()
        with rig.controller.lock:
            rig.controller.errors['oracle_padding'] = 'y' * 200000
        watcher = self.subscribe(rig)
        self.first_snapshot(watcher)
        rig.request('room_volume', {'value': 1})
        rig.controller.worker.submit(lambda: None).result(3)
        time.sleep(.3)
        fds = len(os.listdir('/proc/self/fd'))
        threads = threading.active_count()
        for index in range(40):
            gone = Subscriber(rig.path, read=False, rcvbuf=4096)
            if index % 2:
                gone.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
            rig.request('room_volume', {'value': index + 2})
            gone.close()
        started = time.monotonic()
        rig.request('room_volume', {'value': 99})
        self.assertLess(time.monotonic() - started, .25)
        self.assertIsNotNone(watcher.wait_snapshot(lambda s: s['room_volume'] == 99, .5)[1])
        rig.request('room_volume', {'value': 98})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (len(os.listdir('/proc/self/fd')) > fds + 2 or threading.active_count() > threads + 2):
            time.sleep(.1)
        self.assertLessEqual(len(os.listdir('/proc/self/fd')), fds + 2, 'sockets of vanished subscribers leaked')
        self.assertLessEqual(threading.active_count(), threads + 2, 'threads of vanished subscribers leaked')


# =========================================================================== criterion 5: guarantees

class HardeningOracles(Base):
    def test_socket_is_private(self):
        rig = self.rig()
        self.assertEqual(rig.path.stat().st_mode & 0o077, 0)
        self.assertEqual(rig.path.parent.stat().st_mode & 0o077, 0)

    def test_subscribe_from_another_user_is_refused(self):
        rig = self.rig()
        real = os.getuid()
        with mock.patch('os.getuid', return_value=real + 1):
            subscriber = self.subscribe(rig)
            t, obj = subscriber.wait(lambda o: True, 2)
            subscriber.closed.wait(2)
        self.assertEqual(subscriber.snapshots(), [], 'a foreign peer received snapshots')
        self.assertTrue(is_error(obj) or (obj is None and subscriber.closed.is_set()), obj)

    def test_oversized_or_incomplete_subscribe_requests_are_refused(self):
        rig = self.rig()
        big = json.dumps({'action': 'subscribe', 'args': {'pad': 'x' * (MAX_MESSAGE + 10)}}).encode() + b'\n'
        subscriber = self.subscribe(rig, payload=big)
        subscriber.closed.wait(5)
        self.assertEqual(subscriber.snapshots(), [])
        self.assertTrue(any(is_error(o) for _, o in subscriber.lines) or subscriber.closed.is_set())
        for payload in (b'{"action":"subscribe","args":[1]}\n', b'{"action":"subscribe","args":"x"}\n'):
            subscriber = self.subscribe(rig, payload=payload)
            subscriber.closed.wait(4)
            self.assertEqual(subscriber.snapshots(), [], payload)
        incomplete = self.subscribe(rig, payload=b'{"action":"subscribe"')
        t, obj = incomplete.wait(lambda o: True, 5)
        self.assertEqual(incomplete.snapshots(), [])
        self.assertTrue(incomplete.closed.wait(5), 'an incomplete request held its connection open')

    def test_streamed_lines_never_exceed_the_message_limit(self):
        rig = self.rig()
        subscriber = self.subscribe(rig)
        self.first_snapshot(subscriber)
        with rig.controller.lock:
            rig.controller.errors['oracle_padding'] = 'z' * (MAX_MESSAGE + 100)
        rig.request('room_volume', {'value': 31})
        time.sleep(.5)
        self.assertTrue(all(length <= MAX_MESSAGE for length in subscriber.raw_lengths),
                        'streamed a line of %d bytes' % max(subscriber.raw_lengths))
        with rig.controller.lock:
            del rig.controller.errors['oracle_padding']
        rig.request('room_volume', {'value': 32})
        if not subscriber.closed.is_set():
            subscriber.wait_snapshot(lambda s: s['room_volume'] == 32, 1)
        fresh = self.subscribe(rig)
        self.assertEqual(self.first_snapshot(fresh)['room_volume'], 32)
        self.assertTrue(rig.request('inspect')['ok'])

    def test_no_gtk_or_dbus_in_the_controller_or_client(self):
        code = ('import sys; import room_session, session_client;'
                'bad=[m for m in sys.modules if m.split(".")[0] in ("gi","dbus","pydbus","dasbus")]; print(bad); sys.exit(1 if bad else 0)')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PYTHONPATH=str(SRC)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source = (SRC / 'room_session.py').read_text()
        self.assertNotRegex(source, r'gi\.repository|\bGio\b|\bGLib\b|\bGtk\b|dbus')

    def test_ensure_service_against_a_running_controller_only_inspects(self):
        rig = self.rig()
        with mock.patch.object(session_client.subprocess, 'Popen') as popen, \
                mock.patch.object(session_client.subprocess, 'run') as run:
            session_client.ensure_service(rig.path, timeout=2)
        popen.assert_not_called()
        run.assert_not_called()
        self.assertEqual({n for n in rig.action_names() if not READ_ONLY.match(n)}, set())


class GuaranteeOracles(Base):
    def test_quiet_and_stop_bypass_queued_work_and_cancel_it(self):
        for action in ('stop', 'quiet'):
            with self.subTest(action=action):
                rig = self.rig(path=self.socket_dir / (action + '.sock'), directory=self.state_dir / action)
                rig.backend.block = threading.Event()
                self.addCleanup(rig.backend.block.set)
                rig.request('enter', {'room_id': 'library'})
                deadline = time.monotonic() + 2
                while not rig.backend.calls and time.monotonic() < deadline:
                    time.sleep(.01)
                queued = rig.request('layer', {'layer': 'music', 'action': 'volume', 'value': 12})
                self.assertTrue(queued['result'].get('accepted'))
                generation = rig.controller.generation
                started = time.monotonic()
                response = rig.request(action)
                self.assertLess(time.monotonic() - started, .3, '%s waited for queued work' % action)
                self.assertTrue(response['ok'], response)
                self.assertGreater(rig.controller.generation, generation)
                rig.backend.block.set()
                rig.controller.worker.submit(lambda: None).result(3)
                rig.controller.worker.submit(lambda: None).result(3)
                self.assertNotIn(('start_music', 'piano'), rig.backend.calls)
                self.assertFalse([c for c in rig.backend.calls if c[:2] == ('music', 'volume')])
                rig.stop()

    def test_radio_starts_keep_the_fade_and_cancel_event(self):
        captured = []

        def start_radio(item, ytdlp=None, cancel_event=None, fade_seconds=0.6, mpv_options=None, trusted_fd=None, trusted_unit=None):
            captured.append({'cancel_event': cancel_event, 'fade_seconds': fade_seconds})
        cancel = threading.Event()
        with mock.patch.object(room_session.hub, 'start_radio', start_radio):
            room_session.Backend().start_music({'id': 'piano', 'name': 'Piano', 'kind': 'radio',
                                                'url': 'https://example.test/piano'}, cancel)
        self.assertEqual(captured, [{'cancel_event': cancel, 'fade_seconds': .65}])

    def test_stored_session_from_main_loads_and_is_written_back_compatibly(self):
        stored = {'version': 1, 'last_session': {'name': 'Midnight library', 'room_id': 'library',
                                                 'music': {'source_id': 'piano', 'name': 'Piano', 'volume': 40, 'muted': False}},
                  'settings': {}, 'recent': [], 'station_history': [], 'station_memory': {}, 'room_volume': 70,
                  'last_entered_at': 1000.0, 'placement': {'chosen': False, 'migrated': True}}
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / 'session.json').write_text(json.dumps(stored))
        rig = self.rig()
        snapshot = rig.request('inspect')['result']
        self.assertEqual(snapshot['room_volume'], 70)
        self.assertEqual(snapshot['last_session']['name'], 'Midnight library')
        rig.request('room_volume', {'value': 65})
        written = json.loads((self.state_dir / 'session.json').read_text())
        self.assertEqual(written['version'], 1)
        self.assertLessEqual(set(stored), set(written))
        self.assertEqual(written['last_session']['music']['source_id'], 'piano')
        self.assertIsNone(rig.controller.active, 'loading stored state started a session')


if __name__ == '__main__':
    unittest.main()
