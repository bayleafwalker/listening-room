"""A late failed picture load must never stop the replacement after Stop."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ambience


class PictureRaceTests(unittest.TestCase):
    def test_stop_then_replacement_waits_for_abandoned_picture_cleanup(self):
        blocked = threading.Event()
        release = threading.Event()
        replacement_launched = threading.Event()
        history, errors = [], []
        service = {'active': None, 'launches': 0}
        lock = threading.Lock()

        def run(args, check=True):
            with lock:
                if args[0] == 'systemd-run':
                    service['launches'] += 1
                    service['active'] = service['launches']
                    history.append(('launch', service['active']))
                    if service['active'] == 2:
                        replacement_launched.set()
                elif args[:3] == ['systemctl', '--user', 'stop']:
                    history.append(('stop', service['active']))
                    service['active'] = None
            return SimpleNamespace(returncode=0, stdout='')

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def connect(self, _path):
                pass

            def settimeout(self, _timeout):
                pass

            def sendall(self, data):
                self.command = json.loads(data)['command']

            def recv(self, _size):
                if self.command[1].endswith('/old.mp4'):
                    blocked.set()
                    if not release.wait(3):
                        raise AssertionError('The test did not release the abandoned load')
                    # Its process was stopped while the read was pending.
                    return b''
                return b'{"event":"file-loaded"}\n{"event":"playback-restart"}\n'

            def makefile(self, _mode):
                values = {'idle-active': False, 'pause': False, 'media-title': 'Replacement'}
                reply = {'request_id': 1, 'error': 'success', 'data': values[self.command[1]]}
                return io.BytesIO(json.dumps(reply).encode() + b'\n')

        def start(name):
            try:
                ambience.start({'id': name, 'name': name, 'url': 'https://example.invalid/' + name + '.mp4'})
            except Exception as error:
                errors.append((name, error))

        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder}), \
                patch.object(ambience, 'STATE_HOME', Path(folder) / 'state'), \
                patch.object(ambience.shutil, 'which', return_value='/fixture/mpv'), \
                patch.object(ambience, '_run', side_effect=run), \
                patch.object(ambience.socket, 'socket', side_effect=lambda *_args: Connection()), \
                patch.object(ambience, '_window', return_value={
                    'address': '0x123', 'class': ambience.APP_IDS['ambience'],
                    'floating': True, 'pinned': False}):
            old = threading.Thread(target=start, args=('old',))
            newer = threading.Thread(target=start, args=('new',))
            old.start()
            try:
                self.assertTrue(blocked.wait(2), 'The first picture never reached its pending read')
                # Exercise the real control path: it must invalidate the old
                # startup even though the socket read has not returned yet.
                ambience.control('stop')
                generation_file = Path(folder) / 'listening-hub' / 'ambience.generation'
                stopped_generation = generation_file.read_text()
                newer.start()
                deadline = time.monotonic() + 2
                while (generation_file.read_text() == stopped_generation
                       and not replacement_launched.is_set() and time.monotonic() < deadline):
                    time.sleep(.005)
                self.assertFalse(replacement_launched.is_set(),
                                 'The replacement launched before the abandoned load finished cleanup')
                self.assertNotEqual(generation_file.read_text(), stopped_generation,
                                    'The replacement did not submit its startup request')
            finally:
                release.set()
                old.join(3)
                if newer.ident is not None:
                    newer.join(3)
            self.assertFalse(old.is_alive() or newer.is_alive(), 'A startup remained blocked')
            self.assertTrue(replacement_launched.is_set(), 'The replacement never launched after cleanup')
            self.assertEqual(service['active'], 2, 'Abandoned cleanup stopped the replacement')
            self.assertEqual([(name, type(error)) for name, error in errors], [('old', ambience.AmbienceError)])
            self.assertEqual(ambience._saved()['ambience']['id'], 'new')
            replacement_index = history.index(('launch', 2))
            self.assertFalse(any(action == 'stop' for action, _owner in history[replacement_index + 1:]),
                             'The abandoned request cleaned up after the new player was launched')


if __name__ == '__main__':
    unittest.main()
