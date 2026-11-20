#!/usr/bin/env python3
"""WP2-PROTOCOL acceptance probe: MPRIS as a consumer of controller changes, on a private D-Bus.

Owned by the acceptance author. Run inside the dev loop, which supplies Xvfb, a private
XDG_RUNTIME_DIR and `dbus-run-session`:

    scripts/listening-dev.sh probe OUTDIR wp2-protocol-acceptance

A real Controller with a fake owned player sits behind the real socket server and the real
`session_service.Mpris`. Checks:

* an external player change seen by refresh() reaches MPRIS PropertiesChanged within 300 ms;
* unchanged refreshes emit no PropertiesChanged (MPRIS is change-driven, not a 1 s poll);
* a room volume change over the socket reaches MPRIS within 300 ms;
* MPRIS Pause controls the owned player and never starts anything;
* a socket subscriber sees the same change while MPRIS is attached (new in WP2).

ADAPT: if the implementer changes how Mpris is constructed, adapt `make_mpris` in place and record why.
No audio, window, account or live session bus is touched. Results go to OUTDIR/wp2-protocol-acceptance.json.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import traceback

SCRATCH = Path(tempfile.mkdtemp(prefix='wp2-mpris-'))
for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
    (SCRATCH / key.lower()).mkdir(mode=0o700)
    os.environ[key] = str(SCRATCH / key.lower())
for key in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE'):
    os.environ.pop(key, None)
SRC = Path(os.environ.get('WP2_ORACLE_SRC') or Path(__file__).resolve().parents[2] / 'src')
sys.path.insert(0, str(SRC))


class Player:
    def __init__(self):
        self.value = {'music': {'status': 'Playing', 'source_id': 'radio-test', 'title': 'An isolated stream',
                                'name': 'Probe radio', 'volume': 25., 'muted': False},
                      'atmosphere': {'status': 'Stopped'}, 'picture': {'status': 'Stopped'}}
        self.calls = []

    def layers(self):
        return copy.deepcopy(self.value)

    def media_details(self):
        return {}

    def music_control(self, action, value=None):
        self.calls.append(('music', action, value))
        if action in ('pause', 'play', 'stop'):
            self.value['music']['status'] = {'pause': 'Paused', 'play': 'Playing', 'stop': 'Stopped'}[action]
        elif action in ('volume', 'muted'):
            self.value['music'][action] = value

    def atmosphere_control(self, action, value=None):
        self.calls.append(('atmosphere', action, value))

    def picture_control(self, action, value=None):
        self.calls.append(('picture', action, value))

    def __getattr__(self, name):
        if name.startswith('start'):
            def refuse(*args, **kwargs):
                self.calls.append((name,))
                raise AssertionError('the probe never starts playback')
            return refuse
        raise AttributeError(name)


def exchange(path, action, args=None):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(json.dumps({'action': action, 'args': args or {}}).encode() + b'\n')
        return json.loads(connection.makefile('rb').readline())


def read_snapshot(reader):
    line = reader.readline()
    assert line, 'the controller closed the subscribe connection'
    return find_snapshot(json.loads(line))


def find_snapshot(obj):
    for candidate in (obj, obj.get('result') if isinstance(obj, dict) else None):
        if isinstance(candidate, dict):
            if 'layers' in candidate and 'room_volume' in candidate:
                return candidate
            if isinstance(candidate.get('snapshot'), dict):
                return candidate['snapshot']
    return None


def make_mpris(session_service, service):
    return session_service.Mpris(service)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--repo')
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    from gi.repository import Gio, GLib
    import room_session
    import session_service

    player = Player()
    runtime = SCRATCH / 'run'
    runtime.mkdir(mode=0o700)
    path = runtime / 'listening-hub' / 'session.sock'
    controller = room_session.Controller(player, SCRATCH / 'session')
    controller.active = {'id': 'probe', 'name': 'Probe room', 'music': {'source_id': 'radio-test', 'volume': 25},
                         'atmosphere': {'kind': 'none'}, 'picture': {'enabled': False}}
    controller.refresh()
    service = session_service.Service(controller)
    server, descriptor = session_service.make_server(path, service)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    make_mpris(session_service, service)
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    signals, lock = [], threading.Lock()

    def on_signal(_connection, _sender, _path, _interface, _name, parameters):
        _, changed, _ = parameters.unpack()
        with lock:
            signals.append((time.monotonic(), changed))
    connection.signal_subscribe(session_service.MPRIS_NAME, 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                                session_service.MPRIS_PATH, None, Gio.DBusSignalFlags.NONE, on_signal)
    loop = GLib.MainLoop()
    results = []

    def call(interface, method, arguments=None, result=None):
        return connection.call_sync(session_service.MPRIS_NAME, session_service.MPRIS_PATH, interface, method, arguments,
                                    GLib.VariantType.new(result) if result else None, Gio.DBusCallFlags.NONE, 1000, None)

    def values():
        return call('org.freedesktop.DBus.Properties', 'GetAll',
                    GLib.Variant('(s)', (session_service.PLAYER_INTERFACE,)), '(a{sv})').unpack()[0]

    def wait_signal(since, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                for t, changed in signals:
                    if t >= since and predicate(changed):
                        return t
            time.sleep(.005)
        return None

    def check(name, function):
        try:
            detail = function()
            results.append({'check': name, 'ok': True, 'detail': detail})
            print('PASS', name, detail or '')
        except Exception as error:
            results.append({'check': name, 'ok': False, 'detail': '%s: %s' % (type(error).__name__, error),
                            'trace': traceback.format_exc()})
            print('FAIL', name, '%s: %s' % (type(error).__name__, error))

    def external_change():
        player.value['music']['status'] = 'Paused'
        started = time.monotonic()
        controller.refresh()
        t = wait_signal(started, lambda c: c.get('PlaybackStatus') == 'Paused', .3)
        assert t is not None, 'no PlaybackStatus=Paused signal within 300 ms of refresh'
        return 'latency %.1f ms' % ((t - started) * 1000)

    def idle_quiet():
        time.sleep(.3)
        started = time.monotonic()
        for _ in range(5):
            controller.refresh()
            time.sleep(.1)
        time.sleep(.3)
        with lock:
            spurious = [c for t, c in signals if t >= started]
        assert not spurious, 'unchanged refreshes emitted %d PropertiesChanged: %r' % (len(spurious), spurious[:2])

    def room_volume():
        started = time.monotonic()
        response = exchange(path, 'room_volume', {'value': 60})
        assert response['ok'], response
        t = wait_signal(started, lambda c: True, .3)
        assert t is not None, 'no PropertiesChanged within 300 ms of a room volume change'
        return 'latency %.1f ms' % ((t - started) * 1000)

    def pause_method():
        player.value['music']['status'] = 'Playing'
        controller.refresh()
        time.sleep(.1)
        call(session_service.PLAYER_INTERFACE, 'Pause')
        deadline = time.monotonic() + 2
        while values()['PlaybackStatus'] != 'Paused':
            assert time.monotonic() < deadline, 'MPRIS Pause did not reach the owned player'
            time.sleep(.03)
        assert ('music', 'pause', None) in player.calls, player.calls
        assert not [c for c in player.calls if c[0].startswith('start')], player.calls

    def subscriber_alongside():
        with socket.socket(socket.AF_UNIX) as sub:
            sub.settimeout(2)
            sub.connect(str(path))
            sub.sendall(b'{"action":"subscribe","args":{}}\n')
            reader = sub.makefile('rb')
            first = None
            deadline = time.monotonic() + 2
            while first is None and time.monotonic() < deadline:
                first = read_snapshot(reader)
            assert first is not None, 'no snapshot from subscribe'
            started = time.monotonic()
            exchange(path, 'room_volume', {'value': 44})
            while True:
                assert time.monotonic() - started < 1, 'change not streamed within 1 s'
                snap = read_snapshot(reader)
                if snap and snap.get('room_volume') == 44:
                    return 'latency %.1f ms' % ((time.monotonic() - started) * 1000)

    def run():
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    current = values()
                    break
                except GLib.Error:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.05)
            assert current['PlaybackStatus'] == 'Playing', current
            for name, function in (('mpris_external_change_on_refresh', external_change), ('mpris_idle_refresh_quiet', idle_quiet),
                                   ('mpris_room_volume_over_socket', room_volume), ('mpris_pause_never_starts', pause_method),
                                   ('subscribe_alongside_mpris', subscriber_alongside)):
                check(name, function)
        except Exception:
            results.append({'check': 'setup', 'ok': False, 'detail': traceback.format_exc()})
            print('FAIL setup', traceback.format_exc())
        finally:
            GLib.idle_add(loop.quit)

    threading.Thread(target=run, daemon=True).start()
    GLib.timeout_add_seconds(30, lambda: (results.append({'check': 'timeout', 'ok': False}), loop.quit(), False)[-1])
    loop.run()
    server.shutdown()
    server.server_close()
    controller.close()
    os.close(descriptor)
    passed = sum(1 for r in results if r['ok'])
    (out / 'wp2-protocol-acceptance.json').write_text(json.dumps(results, indent=2))
    print('WP2 protocol probe: %d passed, %d failed' % (passed, len(results) - passed))
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
