"""netguard covers a Python child process a suite starts, not only the suite itself."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import netguard  # noqa: E402

# Documentation address that is not loopback and is never routed; a blocked connect fails before any packet.
OUTSIDE = '192.0.2.1'
CONNECT = ('import socket, sys\n'
           'try:\n'
           '    socket.create_connection((%r, 9), timeout=1)\n'
           'except Exception as error:\n'
           '    print(type(error).__name__)\n'
           'else:\n'
           '    print("connected")\n') % OUTSIDE


def run_child(code, **options):
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30, **options)


class ChildProcesses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # run_suites installs the guard; when this file runs alone, install it as the runner would.
        netguard.install()

    def test_a_child_python_cannot_connect_outside_loopback(self):
        result = run_child(CONNECT)
        self.assertEqual(result.stdout.strip(), 'NetworkBlocked', result.stdout + result.stderr)

    def test_a_child_given_its_own_environment_is_guarded_too(self):
        bare = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/')}
        result = run_child(CONNECT, env=bare)
        self.assertEqual(result.stdout.strip(), 'NetworkBlocked', result.stdout + result.stderr)

    def test_a_child_can_still_use_loopback_and_its_own_site_customisation(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        accepted = []
        threading.Thread(target=lambda: accepted.append(server.accept()[0]), daemon=True).start()
        port = server.getsockname()[1]
        try:
            result = run_child('import socket; socket.create_connection(("127.0.0.1", %d), timeout=2); print("ok")' % port)
        finally:
            server.close()
            for connection in accepted:
                connection.close()
        self.assertEqual(result.stdout.strip(), 'ok', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
