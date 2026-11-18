"""Real systemd/mpv transport and mix lifecycle probe, not a live Twitch test.

Run: python tests/probes/listening-hub-mix.py [path/to/listening-hub/source]
Requires an accessible user systemd bus, mpv and wpctl. Uses null audio output,
local HTTP WAVs, private state/sockets and uniquely named transient units.
"""
import functools
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import wave

source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / 'src'
sys.path.insert(0, str(source))
import listening_hub as hub
import audio_layers as layer

mpv = shutil.which('mpv')
if not mpv:
    raise SystemExit('mpv is required')
identity = 'listening-mix-probe-' + uuid.uuid4().hex
radio_unit = identity + '-radio.service'
overlay_unit = identity + '-overlay.service'
real_run = hub.run

def isolated_run(args, **kwargs):
    # Restrict replacements to exact unit arguments; no normal unit is touched.
    replacements = {'listening-hub-radio.service': radio_unit,
                    '--unit=listening-hub-radio': '--unit=' + radio_unit}
    return real_run([replacements.get(arg, arg) for arg in args], **kwargs)

hub.run = isolated_run
layer.UNIT = overlay_unit
# This bypass is restricted to this local transport/lifecycle probe. Production
# Twitch URL validation and Twitch/Helix behaviour are not exercised here.
layer._url = lambda url: url

with tempfile.TemporaryDirectory(prefix='listening-mix-') as directory:
    work = Path(directory)
    os.environ['XDG_STATE_HOME'] = str(work / 'state-home')
    hub.STATE = work / 'hub-state'
    hub.RUNTIME = work / 'runtime'
    hub.RUNTIME.mkdir(mode=0o700)
    hub.SOCKET = hub.RUNTIME / 'radio.sock'
    layer._runtime = lambda: hub.RUNTIME / 'overlay.sock'
    shim = work / 'mpv'
    shim.write_text('#!' + sys.executable + '\nimport os, sys\nos.execv(' + repr(mpv) + ', [' + repr(mpv) + ', "--ao=null"] + sys.argv[1:])\n')
    shim.chmod(0o700)
    # The fixture is directly playable, so the required resolver is never used.
    resolver = work / 'yt-dlp'
    resolver.write_text('#!/bin/sh\nexit 1\n')
    resolver.chmod(0o700)
    os.environ['PATH'] = str(work) + os.pathsep + os.environ['PATH']
    for name in ('one.wav', 'two.wav'):
        with wave.open(str(work / name), 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0' * 8000 * 60)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(work))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    urls = [f'http://127.0.0.1:{server.server_port}/{name}.wav' for name in ('one', 'two')]

    def command(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    def pid(unit):
        result = int(command('systemctl', '--user', 'show', unit, '--property=MainPID', '--value'))
        assert result > 0, (unit, result)
        return result

    def master():
        return command('wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@')

    def mix():
        assert hub.ipc(['get_property', 'volume']) == 31
        assert hub.ipc(['get_property', 'mute']) is True
        assert layer._ipc(['get_property', 'volume']) == 14
        assert layer._ipc(['get_property', 'mute']) is True

    def radio(url):
        hub.start_radio({'id': 'probe', 'name': 'probe', 'url': url})

    try:
        initial_master = master()
        radio(urls[0])
        hub.selected_target = lambda target_id=None: {'id': 'radio'}
        hub.set_volume(31, target_id='radio')
        hub.mute(target_id='radio')
        layer.start_layer(urls[0], timeout=15)
        layer.control('volume', 14)
        layer.control('mute')
        mix()
        radio_pid, overlay_pid = pid(radio_unit), pid(overlay_unit)
        layer.start_layer(urls[1], timeout=15)
        assert pid(radio_unit) == radio_pid
        assert pid(overlay_unit) != overlay_pid
        mix()
        overlay_pid = pid(overlay_unit)
        radio(urls[1])
        assert pid(overlay_unit) == overlay_pid
        assert pid(radio_unit) != radio_pid
        mix()
        cancelled = threading.Event()
        cancelled.set()
        try:
            layer.start_layer(urls[0], cancel_event=cancelled)
            raise AssertionError('Pre-cancelled start succeeded')
        except layer.LayerError as error:
            assert 'cancelled' in str(error)
        assert pid(overlay_unit) == overlay_pid
        mix()
        assert master() == initial_master
        print('PASS: local HTTP playback, independent replacement, retained mix, pre-cancel and unchanged master volume')
    finally:
        for unit in (radio_unit, overlay_unit):
            subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, text=True, check=False)
        server.shutdown()
        server.server_close()
        thread.join()
