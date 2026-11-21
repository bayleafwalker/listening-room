"""Real decoder EOF semantics, using silent local media and a null audio output."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import wave


class DecoderEOF(unittest.TestCase):
    def test_keep_open_retains_a_positive_eof_without_restarting(self):
        player = os.environ.get('LISTENING_TEST_MPV') or shutil.which('mpv')
        if not player:
            self.skipTest('mpv is unavailable')
        with tempfile.TemporaryDirectory(prefix='lr-eof-') as directory:
            folder = Path(directory)
            sample, endpoint = folder / 'silence.wav', folder / 'player.sock'
            with wave.open(str(sample), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b'\0\0' * 2400)
            environment = dict(os.environ, HOME=directory, XDG_CONFIG_HOME=directory,
                               XDG_STATE_HOME=directory, XDG_CACHE_HOME=directory)
            process = subprocess.Popen([player, '--no-config', '--no-video', '--ao=null',
                '--idle=yes', '--keep-open=yes', '--input-terminal=no',
                '--input-ipc-server=' + str(endpoint), str(sample)],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 8
                while not endpoint.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail('The isolated decoder failed to open its IPC socket')
                    time.sleep(.02)
                def property_value(name):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(1)
                        connection.connect(str(endpoint))
                        connection.sendall(json.dumps({'command': ['get_property', name], 'request_id': 7}).encode() + b'\n')
                        with connection.makefile('rb') as responses:
                            for line in responses:
                                response = json.loads(line)
                                if response.get('request_id') == 7:
                                    if response.get('error') == 'property unavailable':
                                        return None
                                    self.assertEqual(response.get('error'), 'success')
                                    return response.get('data')
                # mpv creates the socket path when it binds, a moment before it listens, so the path
                # existing is not readiness: wait until the decoder accepts a connection.
                while True:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        probe.connect(str(endpoint))
                        break
                    except ConnectionRefusedError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail('The isolated decoder never accepted its IPC socket')
                        time.sleep(.02)
                    finally:
                        probe.close()
                while property_value('eof-reached') is not True:
                    if time.monotonic() >= deadline:
                        self.fail('The decoder did not retain a positive EOF')
                    time.sleep(.02)
                self.assertIsNone(process.poll(), 'A completed seekable item must retain its owned player')
                self.assertFalse(property_value('idle-active'))
                # mpv builds differ in whether keep-open also flips pause;
                # positive EOF is the controller's completion signal.
                time.sleep(.1)
                self.assertTrue(property_value('eof-reached'), 'EOF must remain observable until the controller advances')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    unittest.main()
