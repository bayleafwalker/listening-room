"""Ephemeral, independently owned player for presenter speech slots."""
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import mpv_ipc
import presenter


def _run(args, check=True):
    return subprocess.run(args, check=check)


def play(path, level, muted, maximum):
    """Start a file on its own player, hard-limited to the budgeted slot length."""
    path = Path(path)
    if not path.is_file() or not shutil.which('mpv'):
        raise ValueError('Presenter audio unavailable')
    identity = uuid.uuid4().hex[:12]
    socket = presenter.runtime_dir() / ('player-' + identity + '.sock')
    unit = 'listening-hub-presenter-' + identity + '.service'
    command = mpv_ipc.audio_player(shutil.which('mpv'), socket, 'ListeningRoomPresenter') + [
        '--ytdl=no', '--length=' + str(maximum)]
    mpv_ipc.spawn(_run, unit, command)
    try:
        mpv_ipc.load(socket, str(path), time.monotonic() + 5, lambda: False)
        mpv_ipc.call(socket, ['set_property', 'volume', float(level)], 1)
        mpv_ipc.call(socket, ['set_property', 'mute', bool(muted)], 1)
    except Exception:
        mpv_ipc.stop(_run, unit)
        raise ValueError('Presenter audio unavailable') from None
    return {'unit': unit, 'socket': str(socket)}


def stop(unit):
    if unit:
        mpv_ipc.stop(_run, unit['unit'] if isinstance(unit, dict) else unit)


def control(player, action, value):
    if not isinstance(player, dict) or not player.get('socket'):
        return
    prop = {'level': 'volume', 'muted': 'mute', 'pause': 'pause', 'play': 'pause'}[action]
    if action in ('pause', 'play'):
        value = action == 'pause'
    mpv_ipc.call(Path(player['socket']), ['set_property', prop, value], 1)
