"""Bounded client for the private graphical-session listening controller."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

MAX_MESSAGE = 1024 * 1024


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
