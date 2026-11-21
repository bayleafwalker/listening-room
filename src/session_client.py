"""Bounded client for the private graphical-session listening controller."""
import argparse
import collections
import json
import logging
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import threading
import time

MAX_MESSAGE = 1024 * 1024
PROBE_TIMEOUT = 10   # seconds; the version probe and connect only fail when the controller is unreachable


class SessionUnavailable(ValueError):
    pass


def socket_path():
    override = os.environ.get('LISTENING_SESSION_SOCKET')
    if override:
        return Path(override)
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    if not runtime:
        raise SessionUnavailable('Open Listening Room from your graphical session')
    return Path(runtime) / 'listening-hub' / 'session.sock'


def _exchange(path, action, args, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        payload = json.dumps({'action': action, 'args': args or {}}, allow_nan=False).encode() + b'\n'
        if len(payload) > MAX_MESSAGE:
            raise SessionUnavailable('Listening session request is too large')
        connection.sendall(payload)
        response = bytearray()
        while b'\n' not in response:
            part = connection.recv(min(65536, MAX_MESSAGE + 1 - len(response)))
            if not part:
                raise SessionUnavailable('The listening controller closed its connection')
            response.extend(part)
            if len(response) > MAX_MESSAGE:
                raise SessionUnavailable('Listening session response is too large')
        result = json.loads(response.split(b'\n', 1)[0])
        if not result.get('ok'):
            raise SessionUnavailable(result.get('error') or 'The listening action failed')
        return result['result']


def ensure_service(path=None, timeout=3):
    """Start idle infrastructure only. Neither startup path starts playback."""
    path = Path(path or socket_path())
    try:
        _exchange(path, 'inspect', {}, min(.3, timeout))
        return
    except (OSError, ValueError):
        pass
    if not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')):
        raise SessionUnavailable('The listening controller is not running; open a graphical session')
    try:
        subprocess.run(['systemctl', '--user', 'start', '--no-block', 'listening-session.service'],
                       capture_output=True, timeout=1, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + min(.7, timeout / 2)
    while time.monotonic() < deadline:
        try:
            _exchange(path, 'inspect', {}, .2)
            return
        except (OSError, ValueError):
            time.sleep(.05)
    # A source checkout or first Home Manager activation may lack the unit.
    # The server takes a flock before binding, so simultaneous callers converge.
    script = Path(__file__).with_name('session_service.py')
    subprocess.Popen([sys.executable, str(script), '--socket', str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + max(.2, timeout - .7)
    while time.monotonic() < deadline:
        try:
            _exchange(path, 'inspect', {}, .2)
            return
        except (OSError, ValueError):
            time.sleep(.05)
    raise SessionUnavailable('Listening controller unavailable; retry or check listening-session.service')


def request(action, args=None, timeout=3, start=True):
    path = socket_path()
    try:
        return _exchange(path, action, args, timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        if not start:
            raise SessionUnavailable('The listening controller is not running') from None
        ensure_service(path, timeout)
        return _exchange(path, action, args, timeout)
    except (OSError, json.JSONDecodeError) as error:
        raise SessionUnavailable('Listening controller unavailable: ' + str(error)) from error


Update = collections.namedtuple('Update', 'snapshot revision session connected error')
Update.__doc__ = """One subscription event.

connected=True: `snapshot` is the controller snapshot (only the requested `fields` when
given) at `revision`. Revisions increase within one controller `session`; a gap means
changes were coalesced or filtered out, and a new `session` means the controller restarted.
connected=False: the controller is unreachable; `error` says why and the rest are None.
"""


class Subscription:
    """Iterate controller snapshots as they change, reconnecting until close().

    for update in Subscription(fields=['active', 'layers']):
        ...

    Yields an Update whenever the snapshot (or its `fields` projection) changes, and
    one connected=False Update per outage. While unreachable it retries with jittered
    exponential backoff (`backoff` = first and longest delay in seconds). It never
    starts playback: with start=True it calls ensure_service(), which starts only idle
    infrastructure; by default it only waits for the controller. A silent connection
    is dropped after three missed heartbeats. close() is safe from any thread and ends
    the iteration.
    """
    def __init__(self, fields=None, path=None, start=False, backoff=(.25, 10.)):
        self.fields = list(fields) if fields is not None else None
        self.path = path
        self.start = start
        self.backoff = backoff
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._socket = None

    def close(self):
        self._closed.set()
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def __iter__(self):
        delay, online = self.backoff[0], None
        while not self._closed.is_set():
            try:
                path = Path(self.path or socket_path())
                if self.start:
                    ensure_service(path)
                for update in self._stream(path):
                    delay, online = self.backoff[0], True
                    yield update
                error = 'The listening controller closed its connection'
            except (OSError, ValueError) as failure:
                error = str(failure) or type(failure).__name__
            if self._closed.is_set():
                return
            if online is not False:
                online = False
                yield Update(None, None, None, False, error)
            self._closed.wait(delay * random.uniform(.8, 1.2))
            delay = min(delay * 2, self.backoff[1])

    def _stream(self, path):
        # A controller older than protocol 2 would queue an unknown `subscribe`
        # and report it as a room error, so ask for its version first. An empty
        # `fields` list makes a protocol-2 controller answer with just its version,
        # without a snapshot or its lock, so the probe is cheap whatever the load or
        # snapshot size, and the timeout only has to cover a busy host. A protocol-1
        # controller ignores the argument and returns its whole snapshot, which has no
        # version (or is too large to read): either way it is too old.
        try:
            version = _exchange(path, 'inspect', {'fields': []}, PROBE_TIMEOUT).get('version')
        except SessionUnavailable as failure:
            if 'too large' not in str(failure):
                raise
            version = None
        if not isinstance(version, int):
            raise SessionUnavailable('The listening controller is older than this client; restart listening-session.service')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self._lock:
            if self._closed.is_set():
                connection.close()
                return
            self._socket = connection
        try:
            connection.settimeout(PROBE_TIMEOUT)
            connection.connect(str(path))
            args = {} if self.fields is None else {'fields': self.fields}
            connection.sendall(json.dumps({'action': 'subscribe', 'args': args}).encode() + b'\n')
            connection.settimeout(45)
            buffer = bytearray()
            while not self._closed.is_set():
                newline = buffer.find(b'\n')
                if newline < 0:
                    if len(buffer) > MAX_MESSAGE:
                        raise SessionUnavailable('Listening session response is too large')
                    part = connection.recv(65536)
                    if not part:
                        return
                    buffer.extend(part)
                    continue
                message = json.loads(buffer[:newline])
                del buffer[:newline + 1]
                if not message.get('ok'):
                    raise SessionUnavailable(message.get('error') or 'The listening subscription failed')
                result = message.get('result') or {}
                if result.get('event') == 'snapshot':
                    connection.settimeout(3 * result.get('heartbeat', 15))
                    yield Update(result['snapshot'], result['revision'], result['session'], True, None)
        finally:
            with self._lock:
                self._socket = None
            connection.close()


def subscribe(callback, fields=None, path=None, start=False, backoff=(.25, 10.)):
    """Call `callback(update)` for each Subscription update on a daemon thread.

    Returns the Subscription; close() stops it. The callback runs on that thread, so
    a GTK client hands updates to its main loop (GLib.idle_add). Exceptions from the
    callback are logged and the subscription continues.
    """
    subscription = Subscription(fields, path, start, backoff)
    def run():
        for update in subscription:
            try:
                callback(update)
            except Exception:
                logging.getLogger('listening-room-session').exception('subscription callback failed')
    threading.Thread(target=run, name='listening-subscription', daemon=True).start()
    return subscription


def main():
    parser = argparse.ArgumentParser(description='Control the shared Listening Room session')
    parser.add_argument('action', nargs='?', default='inspect')
    parser.add_argument('arguments', nargs='?', default='{}', help='JSON argument object')
    parser.add_argument('--no-start', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(request(args.action, json.loads(args.arguments), start=not args.no_start), ensure_ascii=False))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
