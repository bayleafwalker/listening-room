"""The owned mpv players: spawn, handshake, JSON-IPC and stop.

Every player (radio, background layer, picture window) is an mpv process in a
transient user unit with a private IPC socket. `spawn` starts one, `load` waits
until it plays, `call` sends it a command and `stop`/`ensure_stopped` end it.
The unit commands go through the caller's `run`, so each module keeps its own
error wording.

`call` opens a connection, sends one command and closes it. Inside
`with shared():` calls from the same thread reuse one connection per socket
path (and remember a failed connect), so a refresh that reads several
properties from one player opens one connection, not one per property. The
scope is thread-local and never outlives the `with` block.
"""
import contextlib
import json
import socket
import threading
import time


class Rejected(Exception):
    """mpv answered, but not with success (the connection stays usable)."""


class Closed(Exception):
    """mpv closed the connection before it answered."""


_local = threading.local()


class _Connection:
    def __init__(self, path, timeout):
        self.requests = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            self.reader = self.sock.makefile('rb')
        except BaseException:
            self.sock.close()
            raise

    def request(self, command, timeout):
        self.sock.settimeout(timeout)
        self.requests += 1
        identity = self.requests
        self.sock.sendall((json.dumps({'command': command, 'request_id': identity}) + '\n').encode())
        for line in self.reader:
            message = json.loads(line)
            if message.get('request_id') == identity:
                if message.get('error') != 'success':
                    raise Rejected(message.get('error'))
                return message.get('data')
        raise Closed()

    def close(self):
        for item in (self.reader, self.sock):
            try:
                item.close()
            except OSError:
                pass


def _one_shot(path, command, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall((json.dumps({'command': command, 'request_id': 1}) + '\n').encode())
        for line in connection.makefile('rb'):
            message = json.loads(line)
            if message.get('request_id') == 1:
                if message.get('error') != 'success':
                    raise Rejected(message.get('error'))
                return message.get('data')
    raise Closed()


def call(path, command, timeout=1):
    """Return mpv's data for `command`; raise OSError, ValueError, Rejected or Closed."""
    pool = getattr(_local, 'pool', None)
    if pool is None:
        return _one_shot(path, command, timeout)
    key = str(path)
    connection = pool.get(key)
    if connection is None:
        try:
            connection = pool[key] = _Connection(path, timeout)
        except OSError as error:
            pool[key] = error
            raise
    if isinstance(connection, OSError):
        raise OSError(connection.errno, str(connection))
    try:
        return connection.request(command, timeout)
    except Rejected:
        raise
    except (OSError, ValueError, Closed):
        pool.pop(key, None)
        connection.close()
        raise


@contextlib.contextmanager
def shared():
    """Reuse connections for calls made by this thread inside the block."""
    if getattr(_local, 'pool', None) is not None:
        yield
        return
    _local.pool = {}
    try:
        yield
    finally:
        pool, _local.pool = _local.pool, None
        for connection in pool.values():
            if isinstance(connection, _Connection):
                connection.close()


class LoadError(Exception):
    """The player did not reach playback. `reason` is one of 'start' (its socket never
    answered), 'rejected' (loadfile refused), 'closed', 'ended' (end-file) or 'timeout';
    `detail` is mpv's own error text, if any."""

    def __init__(self, reason, detail=''):
        super().__init__(reason)
        self.reason, self.detail = reason, detail or ''


def spawn(run, unit, command):
    """Start `command` as a transient user unit that ends with the graphical session."""
    return run(['systemd-run', '--user', '--collect', '--unit=' + unit,
                '--property=PartOf=graphical-session.target',
                '--property=After=graphical-session.target', '--'] + list(command))


def audio_player(mpv, socket_path, client_name):
    """The start of a windowless player's command line; it starts silent and muted."""
    return [mpv, '--no-config', '--idle=yes', '--no-video', '--input-terminal=no',
            '--audio-client-name=' + client_name, '--input-ipc-server=' + str(socket_path),
            '--volume=0', '--mute=yes']


def load(path, url, deadline, cancelled):
    """Load `url` into the player at `path` and return once it is playing.

    Retries the connection until `deadline` (a time.monotonic() value), sends loadfile
    and waits for file-loaded and then playback-restart; a playlist redirect is not an
    end. `cancelled()` runs before every wait and raises to abandon the start.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        while True:
            cancelled()
            try:
                connection.connect(str(path))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LoadError('start') from None
                time.sleep(.05)
        connection.settimeout(.2)
        connection.sendall((json.dumps({'command': ['loadfile', url, 'replace'], 'request_id': 1}) + '\n').encode())
        buffer, loaded = b'', False
        while time.monotonic() < deadline:
            cancelled()
            connection.settimeout(min(.2, max(.01, deadline - time.monotonic())))
            try:
                part = connection.recv(65536)
            except socket.timeout:
                continue
            if not part:
                raise LoadError('closed')
            buffer += part
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                message = json.loads(line)
                if message.get('request_id') == 1 and message.get('error') != 'success':
                    raise LoadError('rejected', message.get('error'))
                if message.get('event') == 'file-loaded':
                    loaded = True
                elif message.get('event') == 'playback-restart' and loaded:
                    return
                elif message.get('event') == 'end-file' and message.get('reason') != 'redirect':
                    raise LoadError('ended', message.get('file_error') or message.get('reason'))
        raise LoadError('timeout')


def stop(run, unit, check=False):
    """Ask systemd to stop `unit`; True when it did (with `check`, `run` raises instead)."""
    return run(['systemctl', '--user', 'stop', unit], check=check).returncode == 0


def ensure_stopped(run, unit):
    """Stop `unit`; True when it is gone, stopped now or never loaded."""
    if stop(run, unit):
        return True
    loaded = run(['systemctl', '--user', 'show', unit, '--property=LoadState', '--value'], check=False)
    return not loaded.returncode and loaded.stdout.strip() == 'not-found'
