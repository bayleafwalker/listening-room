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
import select
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time

import room_session
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


def room_status(snapshot):
    """The room's PlaybackStatus: Playing while any layer sounds, Paused while a paused layer or a resume
    offer waits, else Stopped. The window's Play/Pause button reads the same rule (next_model.room_state)."""
    layers = snapshot.get('layers') or {}
    statuses = [(layers.get(name) or {}).get('status') for name in ('music', 'atmosphere')]
    if 'Playing' in statuses:
        return 'Playing'
    if snapshot.get('active') and ('Paused' in statuses or snapshot.get('resume_offer')):
        return 'Paused'
    return 'Paused' if 'Paused' in statuses else 'Stopped'


def mpris_values(snapshot):
    """Plain values make truthful capability/metadata rules independently testable.

    Play/Pause act on the room (Quiet and Resume), so PlaybackStatus is the room's. Track capabilities
    (Next, Previous, Seek) stay the music layer's own."""
    music = (snapshot.get('layers') or {}).get('music') or {}
    ambience = (snapshot.get('layers') or {}).get('atmosphere') or {}
    active = snapshot.get('active') or {}
    selected = active.get('music') or {}
    station = snapshot.get('station') or {}
    current = station.get('current') or {}
    music_status = music.get('status', 'Stopped')
    if music_status not in ('Playing', 'Paused', 'Stopped'):
        music_status = 'Stopped'
    status = room_status(snapshot)
    has_track = music_status != 'Stopped'
    ambience_on = ambience.get('status') in ('Playing', 'Paused') and ambience.get('kind', 'local') != 'none'
    media = snapshot.get('media') or {}
    # Stream seekability is not proof that live radio offers track seeking.
    source_id = music.get('source_id') or selected.get('source_id') or ''
    seekable = has_track and bool(media.get('seekable')) and (
        source_id == 'youtube-video' or bool(selected.get('reference')))
    metadata = {}
    room = active.get('name')
    if has_track:
        identity = str(source_id) + '\0' + str(current.get('id') or music.get('title', ''))
        title = current.get('name') or music.get('title') or music.get('name') or room or 'Radio'
        metadata = {'mpris:trackid': '/app/kotona/ListeningRoom/track/' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    'xesam:title': title}
        # The artist, or for live radio the station the title plays on.
        source = music.get('station') or music.get('name') or selected.get('name')
        artist = current.get('artist') or music.get('artist') or (source if source and source != title else None)
        album = current.get('album') or music.get('album')
        if artist:
            metadata['xesam:artist'] = [artist]
        if album or room:
            metadata['xesam:album'] = album or room
        if seekable and isinstance(media.get('duration'), (int, float)) and math.isfinite(media['duration']):
            metadata['mpris:length'] = max(0, round(media['duration'] * 1_000_000))
    elif ambience_on and active:
        # Only the ambience sounds: the room is still playing, and says what.
        identity = 'atmosphere\0' + str(ambience.get('source_id') or ambience.get('name') or '')
        metadata = {'mpris:trackid': '/app/kotona/ListeningRoom/track/' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    'xesam:title': ambience.get('name') or ambience.get('title') or 'Ambience',
                    'xesam:artist': ['Ambience']}
        if room:
            metadata['xesam:album'] = room
    if metadata:
        if room:
            # Album may be a library album; the room is always named here too.
            metadata['xesam:comment'] = ['In ' + room]
        artwork = selected.get('artwork_path') or active.get('poster_path')
        if artwork and Path(artwork).is_absolute():
            metadata['mpris:artUrl'] = Path(artwork).as_uri()
    # MPRIS Volume is the room volume. Snapshots without one fall back to the
    # music layer's own level.
    room_volume = snapshot.get('room_volume')
    volume = room_volume if isinstance(room_volume, (int, float)) and not isinstance(room_volume, bool) else music.get('volume')
    volume = max(0., min(1., float(volume) / 100)) if isinstance(volume, (int, float)) and math.isfinite(volume) else 0.
    if room_volume is None and music.get('muted'):
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
            'CanPlay': status != 'Stopped' or bool(active),
            'CanPause': status != 'Stopped', 'CanSeek': seekable, 'CanControl': True}


# Guarantees the action registry must keep: these interrupt or answer at once and never
# wait behind a queued load (Quiet/Stop bypass the queue; rename is a bounded edit).
MUST_RUN_IMMEDIATELY = ('inspect', 'quiet', 'stop', 'resume', 'leave', 'rename', 'room_volume', 'transport')
if not (all(room_session.ACTIONS[name].runs_immediately({}) for name in MUST_RUN_IMMEDIATELY)
        and all(room_session.ACTIONS[name].runs_immediately({'action': stop})
                for name in ('layer', 'picture') for stop in ('pause', 'stop'))):
    raise RuntimeError('The action registry queues an action that must run at once')
# A subscriber that has not taken a line for this long is dropped.
SEND_TIMEOUT = 5


class Service:
    def __init__(self, controller):
        self.controller = controller
        self._feed = None
        self._feed_lock = threading.Lock()

    @property
    def feed(self):
        """The controller's snapshot feed, created on first use (subscribe or MPRIS)."""
        with self._feed_lock:
            if self._feed is None:
                self._feed = room_session.SnapshotFeed(self.controller)
            return self._feed

    def close(self):
        """Ends every subscriber stream."""
        with self._feed_lock:
            if self._feed is not None:
                self._feed.close()

    def _notify(self):
        # Some handlers change saved state without a controller callback.
        if self._feed is not None:
            self._feed.notify()

    def execute(self, action, args):
        spec = room_session.action_spec(action)
        spec.check(args)
        if spec.stream:
            raise SessionError(action + ' streams over the session socket')
        if spec.runs_immediately(args):
            try:
                return self.controller.dispatch(action, args)
            finally:
                self._notify()
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
            finally:
                self._notify()
        self.controller.worker.submit(run)
        return {'accepted': True, 'message': 'Updating room'}


def _line(response):
    encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode() + b'\n'
    return encoded if len(encoded) <= MAX_MESSAGE else b'{"ok":false,"error":"Listening response is too large"}\n'


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
            spec = room_session.action_spec(request['action'])
            if spec.stream:
                spec.check(args)
                return self.subscribe(args)
            response = {'ok': True, 'result': self.server.service.execute(request['action'], args)}
        except Exception as error:
            response = {'ok': False, 'error': str(error)}
        try:
            self.wfile.write(_line(response))
        except (OSError, ValueError):
            pass

    def subscribe(self, args):
        """Stream one line per new revision, coalescing to the latest, plus a heartbeat when idle.

        Each subscriber has its own thread and send timeout, so a slow or vanished
        subscriber only ever delays itself. The subscriber sends nothing after its
        request; input or hang-up ends the stream at once.
        """
        fields = args.get('fields')
        if fields is not None and (len(fields) > 64 or not all(isinstance(f, str) and 0 < len(f) <= 64 for f in fields)):
            raise SessionError('subscribe: fields must be up to 64 snapshot key names')
        feed = self.server.service.feed
        pipe = feed.open()
        try:
            feed.refresh()   # never raises: a failing snapshot leaves the latest good revision
            self.connection.settimeout(SEND_TIMEOUT)
            poller = select.poll()
            poller.register(self.connection.fileno(), select.POLLIN)
            poller.register(pipe[0], select.POLLIN)
            sent, view = None, None
            heartbeat_due = 0
            while not feed.closed:
                try:
                    os.read(pipe[0], 4096)   # drain before reading the latest revision
                except BlockingIOError:
                    pass
                revision, snapshot, encoded = feed.latest()
                body = None
                if snapshot is None:
                    pass   # no snapshot has ever succeeded; wait for the first one
                elif revision != sent:
                    sent = revision
                    if fields is not None:
                        projection = {name: snapshot.get(name) for name in fields}
                        if projection != view:
                            view = projection
                            body = self.event(feed, revision, json.dumps(projection, ensure_ascii=False, allow_nan=False).encode())
                    else:
                        body = self.event(feed, revision, encoded)
                elif time.monotonic() >= heartbeat_due:
                    body = b'{"ok":true,"result":{"event":"heartbeat","revision":%d}}\n' % revision
                if body is not None:
                    self.connection.sendall(body)
                    if body.startswith(b'{"ok":false'):
                        return
                    heartbeat_due = time.monotonic() + room_session.HEARTBEAT_SECONDS
                wait = max(0, heartbeat_due - time.monotonic())
                if snapshot is None:
                    wait = SEND_TIMEOUT
                for descriptor, _ in poller.poll(wait * 1000):
                    if descriptor != pipe[0]:
                        return   # input or hang-up from the subscriber
        except (OSError, ValueError):
            pass   # the subscriber is gone or too slow; others are unaffected
        finally:
            feed.release(pipe)

    @staticmethod
    def event(feed, revision, encoded):
        if encoded is None:
            return b'{"ok":false,"error":"Listening snapshot could not be encoded"}\n'
        head = json.dumps({'event': 'snapshot', 'version': room_session.PROTOCOL_VERSION, 'session': feed.session,
                           'revision': revision, 'heartbeat': room_session.HEARTBEAT_SECONDS})
        line = b'{"ok":true,"result":' + head[:-1].encode() + b',"snapshot":' + encoded + b'}}\n'
        return line if len(line) <= MAX_MESSAGE else b'{"ok":false,"error":"Listening response is too large"}\n'


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
        # Only a new snapshot revision schedules a PropertiesChanged comparison.
        service.feed.add_listener(lambda *_: GLib.idle_add(self.changed))

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
                # Stop leaves the room, as the window's Leave does (Return brings it back).
                if snapshot.get('active'):
                    self.service.execute('leave', {})
            elif method == 'Pause' and values['CanPause']:
                self.service.execute('transport', {'action': 'pause'})
            elif method in ('Next', 'Previous') and values['CanGoNext' if method == 'Next' else 'CanGoPrevious']:
                self.service.execute('layer', {'layer': 'music', 'action': method.lower()})
            elif method in ('Play', 'PlayPause') and values['CanPlay']:
                # The room transport: Pause is Quiet (every layer), Play is Resume, a silent room is entered again.
                self.service.execute('transport', {'action': 'play' if method == 'Play' else 'toggle'})
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
        snapshot = self.service.feed.latest()[1]
        if snapshot is None:   # no snapshot has succeeded yet; the first revision schedules us again
            return False
        plain = mpris_values(snapshot)
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
                changed = controller.errors.get('refresh') != recorded
                controller.errors['refresh'] = recorded
            if changed:
                controller._changed()   # errors are part of the snapshot
            if recorded not in logged:
                if len(logged) >= 50:
                    logged.clear()
                logged.add(recorded)
                LOG.error('room refresh failed: %s', recorded, exc_info=True)
        else:
            if recorded is not None:
                with controller.lock:
                    cleared = controller.errors.get('refresh') == recorded
                    if cleared:
                        del controller.errors['refresh']
                recorded = None
                if cleared:
                    controller._changed()
        try:
            controller.poll_policy()
        except Exception as error:
            status = {'error': str(error)}
            if controller.policy_status != status:
                controller.policy_status = status
                controller._changed()
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
        service.close()
        controller.close()
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        os.close(descriptor)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
