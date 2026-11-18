"""Private Unix RPC and MPRIS facade for a single Listening Room session."""
import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading

from room_session import Controller, SessionError
from session_client import MAX_MESSAGE, socket_path

LOG = logging.getLogger('listening-room-session')
MPRIS_NAME = 'org.mpris.MediaPlayer2.ListeningRoom'
MPRIS_PATH = '/org/mpris/MediaPlayer2'
ROOT_INTERFACE = 'org.mpris.MediaPlayer2'
PLAYER_INTERFACE = ROOT_INTERFACE + '.Player'

MPRIS_XML = '''<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/>
  <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
  <method name="SetPosition"><arg name="TrackId" type="o" direction="in"/><arg name="Position" type="x" direction="in"/></method>
  <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>'''


def mpris_values(snapshot):
    """Plain values make truthful capability/metadata rules independently testable."""
    music = (snapshot.get('layers') or {}).get('music') or {}
    active = snapshot.get('active') or {}
    selected = active.get('music') or {}
    station = snapshot.get('station') or {}
    current = station.get('current') or {}
    status = music.get('status', 'Stopped')
    if status not in ('Playing', 'Paused', 'Stopped'):
        status = 'Stopped'
    has_track = status != 'Stopped'
    media = snapshot.get('media') or {}
    # Stream seekability is not proof that live radio offers track seeking.
    source_id = music.get('source_id') or selected.get('source_id') or ''
    seekable = has_track and bool(media.get('seekable')) and (
        source_id == 'youtube-video' or bool(selected.get('reference')))
    metadata = {}
    if has_track:
        identity = str(source_id) + '\0' + str(current.get('id') or music.get('title', ''))
        metadata = {'mpris:trackid': '/app/kotona/ListeningRoom/track/' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    'xesam:title': current.get('name') or music.get('title') or music.get('name') or active.get('name') or 'Radio'}
        artist = current.get('artist') or music.get('artist')
        album = current.get('album') or music.get('album')
        if artist:
            metadata['xesam:artist'] = [artist]
        if album:
            metadata['xesam:album'] = album
        if seekable and isinstance(media.get('duration'), (int, float)) and math.isfinite(media['duration']):
            metadata['mpris:length'] = max(0, round(media['duration'] * 1_000_000))
        artwork = selected.get('artwork_path') or active.get('poster_path')
        if artwork and Path(artwork).is_absolute():
            metadata['mpris:artUrl'] = Path(artwork).as_uri()
    # MPRIS Volume is the room volume. Snapshots without one fall back to the
    # music layer's own level.
    room = snapshot.get('room_volume')
    volume = room if isinstance(room, (int, float)) and not isinstance(room, bool) else music.get('volume')
    volume = max(0., min(1., float(volume) / 100)) if isinstance(volume, (int, float)) and math.isfinite(volume) else 0.
    if room is None and music.get('muted'):
        volume = 0.
    position = media.get('position')
    position = max(0, round(position * 1_000_000)) if seekable and isinstance(position, (int, float)) and math.isfinite(position) else 0
    queue = selected.get('queue') or []
    index = selected.get('queue_index', 0)
    index = index if isinstance(index, int) and not isinstance(index, bool) else 0
    return {'PlaybackStatus': status, 'Rate': 1., 'Metadata': metadata, 'Volume': volume,
            'Position': position, 'MinimumRate': 1., 'MaximumRate': 1.,
            'CanGoNext': has_track and (bool(station.get('can_next')) or bool(selected.get('reference')) and index + 1 < len(queue)),
            'CanGoPrevious': has_track and bool(selected.get('reference')) and 0 < index < len(queue),
            'CanPlay': has_track or bool(active),
            'CanPause': has_track, 'CanSeek': seekable, 'CanControl': True}


class Service:
    def __init__(self, controller):
        self.controller = controller

    def execute(self, action, args):
        # These calls are bounded state operations or interrupt an in-flight load.
        immediate = action in ('inspect', 'get', 'enter', 'music', 'online', 'return',
                               'quiet', 'resume', 'stop', 'leave', 'wind_down', 'variants', 'delete', 'rename', 'settings',
                               'room_volume')
        immediate = immediate or (action in ('layer', 'picture') and args.get('action') in ('pause', 'stop'))
        # Station handlers persist small local recipes or schedule their own
        # cancellable playback work. Lists and edits must return their actual
        # result rather than disappear behind a generic queued acknowledgement.
        immediate = immediate or action.startswith(('station.', 'metadata.'))
        if immediate:
            return self.controller.dispatch(action, args)
        generation, cancel = self.controller.generation, self.controller.cancel
        queued_args = dict(args, _request_generation=generation, _cancel_event=cancel)
        def run():
            try:
                if generation != self.controller.generation or cancel.is_set():
                    return
                self.controller.dispatch(action, queued_args)
            except Exception as error:
                with self.controller.lock:
                    if generation != self.controller.generation or cancel.is_set():
                        return
                    self.controller.errors[args.get('layer') or ('atmosphere' if action == 'atmosphere' else 'session')] = str(error)
                self.controller._changed()
        self.controller.worker.submit(run)
        return {'accepted': True, 'message': 'Updating room'}


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            if hasattr(socket, 'SO_PEERCRED'):
                import struct
                _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise SessionError('The listening socket belongs to another user')
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                raise SessionError('Listening request is too large or incomplete')
            request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON value')))
            if not isinstance(request, dict) or not isinstance(request.get('action'), str):
                raise SessionError('Choose a listening action')
            args = request.get('args') or {}
            if not isinstance(args, dict):
                raise SessionError('Listening arguments must be an object')
            response = {'ok': True, 'result': self.server.service.execute(request['action'], args)}
        except Exception as error:
            response = {'ok': False, 'error': str(error)}
        try:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode() + b'\n'
            if len(encoded) > MAX_MESSAGE:
                encoded = b'{"ok":false,"error":"Listening response is too large"}\n'
            self.wfile.write(encoded)
        except (OSError, ValueError):
            pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


class Mpris:
    def __init__(self, service):
        import gi
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.service = service
        self.connection = None
        self.previous = {}
        self.owner = Gio.bus_own_name(Gio.BusType.SESSION, MPRIS_NAME, Gio.BusNameOwnerFlags.NONE,
                                     self.acquired, None, self.lost)
        service.controller.callbacks.append(lambda: GLib.idle_add(self.changed))

    def lost(self, *_):
        # RPC remains available if session D-Bus is temporarily unavailable.
        self.connection = None

    def acquired(self, connection, _name):
        self.connection = connection
        node = self.Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        for interface in node.interfaces:
            connection.register_object(MPRIS_PATH, interface, self.method, self.get_property, self.set_property)
        self.changed()

    def variants(self, plain):
        GLib = self.GLib
        result = {}
        for key, value in plain.items():
            if key == 'Metadata':
                values = {}
                for field, item in value.items():
                    kind = 'o' if field == 'mpris:trackid' else 'x' if field == 'mpris:length' else 'as' if isinstance(item, list) else 's'
                    values[field] = GLib.Variant(kind, item)
                result[key] = GLib.Variant('a{sv}', values)
            else:
                kind = 'b' if isinstance(value, bool) else 'd' if isinstance(value, float) else 'x' if isinstance(value, int) else 'as' if isinstance(value, list) else 's'
                result[key] = GLib.Variant(kind, value)
        return result

    def get_property(self, _connection, _sender, _path, interface, name):
        if interface == ROOT_INTERFACE:
            plain = {'CanQuit': False, 'CanRaise': True, 'HasTrackList': False, 'Identity': 'Listening room',
                     'DesktopEntry': 'listening-room', 'SupportedUriSchemes': [], 'SupportedMimeTypes': []}
        else:
            plain = mpris_values(self.service.controller.snapshot())
        return self.variants(plain)[name]

    def set_property(self, _connection, _sender, _path, _interface, name, variant):
        if name == 'Volume':
            value = float(variant.unpack())
            if not math.isfinite(value):
                return False
            self.service.execute('room_volume', {'value': max(0, min(100, value * 100))})
            return True
        return name == 'Rate' and variant.unpack() == 1.

    def method(self, connection, _sender, _path, interface, method, parameters, invocation):
        try:
            controller = self.service.controller
            snapshot = controller.snapshot()
            values = mpris_values(snapshot)
            if interface == ROOT_INTERFACE:
                if method == 'Raise':
                    subprocess.Popen(['listening-room'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif method == 'Stop':
                self.service.execute('layer', {'layer': 'music', 'action': 'stop'})
            elif method == 'Pause' and values['CanPause']:
                self.service.execute('layer', {'layer': 'music', 'action': 'pause'})
            elif method in ('Next', 'Previous') and values['CanGoNext' if method == 'Next' else 'CanGoPrevious']:
                self.service.execute('layer', {'layer': 'music', 'action': method.lower()})
            elif method in ('Play', 'PlayPause') and values['CanPlay']:
                if values['PlaybackStatus'] == 'Stopped' and snapshot.get('active'):
                    self.service.execute('enter', {'recipe': snapshot['active']})
                else:
                    action = 'play' if method == 'Play' else 'toggle'
                    self.service.execute('layer', {'layer': 'music', 'action': action})
            elif method in ('Seek', 'SetPosition') and values['CanSeek']:
                arguments = parameters.unpack()
                if method == 'Seek':
                    offset = arguments[0] / 1_000_000
                else:
                    identity = values['Metadata'].get('mpris:trackid')
                    if arguments[0] != identity or arguments[1] < 0 or arguments[1] > values['Metadata'].get('mpris:length', 0):
                        invocation.return_value(self.GLib.Variant('()', ()))
                        return
                    offset = (arguments[1] - values['Position']) / 1_000_000
                self.service.execute('layer', {'layer': 'music', 'action': 'seek', 'value': offset})
                position = max(0, values['Position'] + round(offset * 1_000_000))
                connection.emit_signal(None, MPRIS_PATH, PLAYER_INTERFACE, 'Seeked', self.GLib.Variant('(x)', (position,)))
            elif method == 'OpenUri':
                raise SessionError('Send a link to Listening Room to preview it first')
            # Next/Previous/Seek intentionally do nothing when unsupported.
            invocation.return_value(self.GLib.Variant('()', ()))
        except Exception as error:
            invocation.return_dbus_error('app.kotona.ListeningRoom.Error', str(error))

    def changed(self):
        if not self.connection:
            return False
        plain = mpris_values(self.service.controller.snapshot())
        changed = {name: value for name, value in plain.items()
                   if name not in ('Position', 'CanControl') and self.previous.get(name) != value}
        self.previous = plain
        if changed:
            self.connection.emit_signal(None, MPRIS_PATH, 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                self.GLib.Variant('(sa{sv}as)', (PLAYER_INTERFACE, self.variants(changed), [])))
        return False


def make_server(path, service):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if stat.S_ISLNK(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise SessionError('Listening runtime directory must be private and owned by your user')
    lock_path = path.with_suffix('.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None, None
    if path.exists() or path.is_symlink():
        existing = path.lstat()
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            os.close(descriptor)
            raise SessionError('Refusing to replace an unexpected listening socket path')
        path.unlink()
    old_mask = os.umask(0o077)
    try:
        server = UnixServer(str(path), RequestHandler)
    except Exception:
        os.close(descriptor)
        raise
    finally:
        os.umask(old_mask)
    server.service = service
    return server, descriptor


def refresh_loop(controller, stopping, interval=1):
    """Refresh and poll the audio policy until `stopping` is set.

    Nothing an iteration raises ends the loop: the error is recorded in
    errors['refresh'], logged once per distinct error, and the next iteration
    runs as usual. A later clean iteration clears the error it recorded.
    """
    logged = set()
    recorded = None
    while not stopping.is_set():
        try:
            controller.refresh()
        except Exception as error:
            recorded = '%s: %s' % (type(error).__name__, error)
            with controller.lock:
                controller.errors['refresh'] = recorded
            if recorded not in logged:
                if len(logged) >= 50:
                    logged.clear()
                logged.add(recorded)
                LOG.error('room refresh failed: %s', recorded, exc_info=True)
        else:
            if recorded is not None:
                with controller.lock:
                    if controller.errors.get('refresh') == recorded:
                        del controller.errors['refresh']
                recorded = None
        try:
            controller.poll_policy()
        except Exception as error:
            controller.policy_status = {'error': str(error)}
            message = 'audio policy poll failed: %s: %s' % (type(error).__name__, error)
            if message not in logged:
                logged.add(message)
                LOG.error(message, exc_info=True)
        stopping.wait(interval)


def main():
    parser = argparse.ArgumentParser(description='Local Listening Room session controller')
    parser.add_argument('--socket')
    parser.add_argument('--no-mpris', action='store_true')
    args = parser.parse_args()
    path = Path(args.socket) if args.socket else socket_path()
    controller = Controller()
    service = Service(controller)
    server, descriptor = make_server(path, service)
    if server is None:
        return 0
    stopping = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    threading.Thread(target=refresh_loop, args=(controller, stopping), name='room-refresh', daemon=True).start()
    def stop(*_):
        stopping.set()
        if loop is not None:
            loop.quit()
    loop = None
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if args.no_mpris:
            stopping.wait()
        else:
            from gi.repository import GLib
            mpris = Mpris(service)
            loop = GLib.MainLoop()
            loop.run()
    finally:
        stopping.set()
        controller.close()
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        os.close(descriptor)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
