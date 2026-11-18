"""One mpv JSON-IPC request, with optional connection reuse inside a scope.

`call` opens a connection, sends one command and closes it, as the callers did
individually before. Inside `with shared():` calls from the same thread reuse
one connection per socket path (and remember a failed connect), so a refresh
that reads several properties from one player opens one connection, not one
per property. The scope is thread-local and never outlives the `with` block.
"""
import contextlib
import json
import socket
import threading


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
