#!/usr/bin/env python3
"""Exercise real Gio MPRIS on an isolated D-Bus with fake owned playback.

Usage: python3 tests/probes/listening-session-mpris.py /nix/store/...-listening-hub
No audio, graphical windows, online accounts or live session bus are touched.
"""
import argparse
import copy
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback


def outer(package, source=None):
    package = Path(package).resolve()
    wrapper = package / 'bin' / '.listening-room-wrapped'
    binary = wrapper.read_bytes()
    matches = re.findall(rb'/nix/store/[^\x00\s\'\"]+-python3-[^\x00\s\'\"]+/bin/python3', binary)
    if not matches:
        raise RuntimeError('Could not identify the packaged Python interpreter')
    python = matches[-1].decode()
    desktop_wrapper = (package / 'bin' / 'listening-room').read_bytes()
    typelib = re.search(rb"--prefix 'GI_TYPELIB_PATH' ':' '([^']+)'", desktop_wrapper)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if typelib:
        env['GI_TYPELIB_PATH'] = typelib.group(1).decode()
    source = str(Path(source).resolve()) if source else str(package / 'share' / 'listening-hub')
    return subprocess.run(['dbus-run-session', '--', python, str(Path(__file__).resolve()),
                           '--child', '--source', source], env=env, timeout=20).returncode


def child(source):
    sys.path.insert(0, source)
    from gi.repository import Gio, GLib
    import room_session
    import session_service

    class Player:
        def __init__(self):
            self.value = {'music': {'status': 'Playing', 'source_id': 'radio-test', 'title': 'An isolated stream',
                                    'name': 'Probe radio', 'volume': 25., 'muted': False},
                          'atmosphere': {'status': 'Stopped'}, 'picture': {'status': 'Stopped'}}
            self.calls = []
        def layers(self):
            return copy.deepcopy(self.value)
        def music_control(self, action, value=None):
            self.calls.append((action, value))
            if action in ('pause', 'play', 'stop'):
                self.value['music']['status'] = {'pause': 'Paused', 'play': 'Playing', 'stop': 'Stopped'}[action]
            elif action == 'volume':
                self.value['music']['volume'] = value
            elif action == 'muted':
                self.value['music']['muted'] = value
        def atmosphere_control(self, action, value=None):
            raise AssertionError('Music MPRIS unexpectedly controlled Atmosphere')
        def picture_control(self, action, value=None):
            raise AssertionError('Music MPRIS unexpectedly controlled Picture')

    player = Player()
    failures, signals = [], []
    with tempfile.TemporaryDirectory(prefix='listening-mpris-') as directory:
        for key, leaf in (('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
                          ('XDG_CACHE_HOME', 'cache'), ('XDG_RUNTIME_DIR', 'runtime')):
            target = Path(directory) / leaf
            target.mkdir(mode=0o700)
            os.environ[key] = str(target)
        controller = room_session.Controller(player, directory)
        controller.active = {'id': 'probe', 'name': 'Probe room', 'music': {'source_id': 'radio-test', 'volume': 25},
                             'atmosphere': {'kind': 'none'}, 'picture': {'enabled': False}}
        controller.refresh()
        mpris = session_service.Mpris(session_service.Service(controller))
        loop = GLib.MainLoop()
        def test():
            try:
                connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                def call(interface, method, arguments=None, result=None):
                    return connection.call_sync(session_service.MPRIS_NAME, session_service.MPRIS_PATH,
                        interface, method, arguments, GLib.VariantType.new(result) if result else None,
                        Gio.DBusCallFlags.NONE, 1000, None)
                def values():
                    return call('org.freedesktop.DBus.Properties', 'GetAll',
                        GLib.Variant('(s)', (session_service.PLAYER_INTERFACE,)), '(a{sv})').unpack()[0]
                deadline = time.monotonic() + 5
                while True:
                    try:
                        current = values()
                        break
                    except GLib.Error:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.05)
                connection.signal_subscribe(session_service.MPRIS_NAME, 'org.freedesktop.DBus.Properties',
                    'PropertiesChanged', session_service.MPRIS_PATH, None, Gio.DBusSignalFlags.NONE,
                    lambda *_: signals.append(True))
                assert current['PlaybackStatus'] == 'Playing', current
                assert current['Volume'] == 1.0, current  # MPRIS Volume is the room volume (default 1.0), not the music layer's 25
                assert not current['CanSeek'] and not current['CanGoNext'] and not current['CanGoPrevious'], current
                assert current['Metadata']['xesam:title'] == 'An isolated stream', current
                assert current['Metadata']['mpris:trackid'].startswith('/app/kotona/ListeningRoom/track/'), current
                call(session_service.PLAYER_INTERFACE, 'Pause')
                assert values()['PlaybackStatus'] == 'Paused'
                before = len(player.calls)
                call(session_service.PLAYER_INTERFACE, 'Next')
                call(session_service.PLAYER_INTERFACE, 'Seek', GLib.Variant('(x)', (1_000_000,)))
                assert len(player.calls) == before, 'Live radio skip/seek must do nothing'
                call('org.freedesktop.DBus.Properties', 'Set',
                     GLib.Variant('(ssv)', (session_service.PLAYER_INTERFACE, 'Volume', GLib.Variant('d', .42))))
                deadline = time.monotonic() + 3
                while values()['Volume'] != .42:
                    if time.monotonic() >= deadline:
                        raise AssertionError('MPRIS volume change did not reach the owned player')
                    time.sleep(.03)
                call(session_service.PLAYER_INTERFACE, 'Play')
                deadline = time.monotonic() + 3
                while values()['PlaybackStatus'] != 'Playing':
                    if time.monotonic() >= deadline:
                        raise AssertionError('MPRIS Play did not resume owned playback')
                    time.sleep(.03)
                # The subscriber is dispatched in this worker's thread-default
                # context, so iterate it explicitly before checking signals.
                context = GLib.MainContext.default()
                deadline = time.monotonic() + .5
                while time.monotonic() < deadline and not signals:
                    context.iteration(False)
                    time.sleep(.01)
                assert signals, 'PropertiesChanged was not emitted'
                print('PASS real Gio MPRIS: discovery, metadata, volume, pause/play, changed signals, live-radio capability guards')
            except Exception:
                failures.append(traceback.format_exc())
            finally:
                GLib.idle_add(loop.quit)
        thread = threading.Thread(target=test, daemon=True)
        thread.start()
        GLib.timeout_add_seconds(15, lambda: (failures.append('MPRIS probe timed out'), loop.quit(), False)[-1])
        loop.run()
        controller.close()
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', nargs='?')
    parser.add_argument('--source')
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    raise SystemExit(child(args.source) if args.child else outer(args.package, args.source))
