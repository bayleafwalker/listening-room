import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import metadata_http


class SlowHeaderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.server = socket.socket()
            self.server.bind(('127.0.0.1', 0))
        except PermissionError:
            if hasattr(self, 'server'):
                self.server.close()
            self.skipTest('loopback sockets unavailable in this sandbox')
        self.server.listen()
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.stopped.set()
        self.server.close()
        self.worker.join(timeout=2)

    def serve(self):
        try:
            conn, _ = self.server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                while not self.stopped.wait(.05):
                    conn.sendall(b'x')
        except OSError:
            pass

    def test_total_deadline_interrupts_trickling_headers(self):
        start = time.monotonic()
        with self.assertRaisesRegex(metadata_http.TransportError, 'timed out'):
            list(metadata_http.exchange('http://127.0.0.1:%d/' % self.server.getsockname()[1], {}, timeout=.4))
        self.assertLess(time.monotonic() - start, 2)

    def test_cancel_interrupts_trickling_headers(self):
        cancelled = threading.Event()
        timer = threading.Timer(.2, cancelled.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaisesRegex(metadata_http.TransportError, 'cancelled'):
                list(metadata_http.exchange('http://127.0.0.1:%d/' % self.server.getsockname()[1], {}, cancel_event=cancelled, timeout=5))
            self.assertLess(time.monotonic() - start, 2)
        finally:
            timer.cancel()
