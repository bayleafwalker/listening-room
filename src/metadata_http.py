"""Private, killable HTTP transport for bounded metadata jobs.

Credentials are passed through a private stdin pipe, never argv or logs. The
parent owns the deadline and kills the child even during DNS, TLS or headers.
"""
import json
import os
import selectors
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

import http_fetch


class TransportError(Exception):
    pass


def exchange(url, headers, payload=None, cancel_event=None, timeout=30):
    """Yield (kind, bytes): H response headers, D body chunks, E end."""
    request = json.dumps({'url': url, 'headers': headers,
                          'payload': payload.decode('utf-8') if payload is not None else None}).encode() + b'\n'
    process = subprocess.Popen([sys.executable, __file__, '--worker'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    buffer = bytearray()
    sent = 0
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, 'write')
        selector.register(process.stdout, selectors.EVENT_READ, 'read')
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TransportError('Request cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError('Request timed out')
            for key, _ in selector.select(min(.1, remaining)):
                if key.data == 'write':
                    try:
                        count = os.write(key.fileobj.fileno(), request[sent:sent + 65536])
                    except (BrokenPipeError, OSError):
                        raise TransportError('Metadata request failed') from None
                    sent += count
                    if sent == len(request):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        data = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        raise TransportError('Metadata request failed')
                    buffer.extend(data)
                    while len(buffer) >= 5:
                        kind = bytes(buffer[:1])
                        length = struct.unpack('!I', buffer[1:5])[0]
                        if length > 65536:
                            raise TransportError('Invalid metadata response')
                        if len(buffer) < 5 + length:
                            break
                        block = bytes(buffer[5:5 + length])
                        del buffer[:5 + length]
                        if kind == b'F':
                            raise TransportError('Metadata service could not complete the request')
                        if kind not in (b'H', b'D', b'E'):
                            raise TransportError('Invalid metadata response')
                        yield kind, block
                        if kind == b'E':
                            return
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout):
            if pipe and not pipe.closed:
                pipe.close()


def _worker():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            fp.close()
            raise ValueError('redirect')
    def send(kind, value=b''):
        sys.stdout.buffer.write(kind + struct.pack('!I', len(value)) + value)
        sys.stdout.buffer.flush()
    try:
        data = json.loads(sys.stdin.buffer.readline(100000))
        payload = data.get('payload')
        request = urllib.request.Request(data['url'], headers=data['headers'],
                                         data=payload.encode() if payload is not None else None)
        try:
            response = http_fetch.open_url(request, 6, refuse=NoRedirect)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            send(b'H', json.dumps({'status': status, 'length': None}).encode())
            send(b'E')
            return
        with response:
            send(b'H', json.dumps({'status': response.status, 'length': response.headers.get('Content-Length')}).encode())
            read = getattr(response, 'read1', response.read)
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                send(b'D', chunk)
        send(b'E')
    except Exception:
        try:
            send(b'F')
        except Exception:
            pass


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    _worker()
