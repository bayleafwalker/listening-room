"""Live graphical-session probe; use a built listening-hub store path as argv[1].

CHECK_YOUTUBE=1 also exercises public YouTube streaming with null audio output.
CHECK_ROOMS=1 checks all offline room loops and full-resolution still artwork.
CHECK_LOOP=1 verifies that the first silent clip wraps during actual playback.
Player units, sockets and saved state are isolated from normal listening windows.
"""
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

for line in subprocess.check_output(['systemctl','--user','show-environment'],text=True).splitlines():
    key,_,value=line.partition('=')
    if key in ('DISPLAY','WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE'): os.environ[key]=value
package = Path(sys.argv[1])
sys.path.insert(0, str(package / 'share/listening-hub'))
wrapper_text = '\n'.join(path.read_bytes().decode('utf-8', errors='ignore') for path in (package / 'bin').iterdir() if path.is_file() and path.name.startswith('.listening-hub'))
runtime_bins = re.findall(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._-]+/bin', wrapper_text)
os.environ['PATH'] = ':'.join(dict.fromkeys(runtime_bins)) + ':' + os.environ['PATH']
spec = importlib.util.spec_from_file_location('view', package / 'share/listening-hub/ambience.py')
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)
original_run = view._run
suffix = str(os.getpid())
for mode in view.MODES:
    view.APP_IDS[mode] += 'Test' + suffix
def run(args, **kwargs):
    args = [arg.replace('listening-hub-ambience.service', 'listening-test-ambience-' + suffix + '.service').replace('listening-hub-video.service', 'listening-test-video-' + suffix + '.service') for arg in args]
    return original_run(args, **kwargs)
view._run = run

with tempfile.TemporaryDirectory(prefix='listening-views-test-') as directory:
    work = Path(directory)
    view.STATE_HOME = work / 'state'
    runtime = work / 'runtime'
    runtime.mkdir(mode=0o700)
    view._runtime = lambda: runtime
    # Keep sound-enabled decoder tests silent without changing application
    # mode flags or the user's actual audio output.
    real_mpv = shutil.which('mpv')
    assert real_mpv
    wrapper = work / 'mpv'
    wrapper.write_text('#!' + shutil.which('python3') + '\nimport os,sys\nos.execv(' + repr(real_mpv) + ', [' + repr(real_mpv) + ', "--ao=null"] + sys.argv[1:])\n')
    wrapper.chmod(0o700)
    os.environ['PATH'] = str(work) + ':' + os.environ['PATH']
    before = json.loads(original_run(['hyprctl', 'activeworkspace', '-j']).stdout)['id']
    try:
        lake = os.environ.get('TEST_VISTA_FILE', str(package / 'share/listening-hub/assets/northern-lake.mp4'))
        view.start({'name':'Test offline lake','url':lake}, placement='9')
        assert view._ipc(['get_property','options/osc']) is False
        assert view._ipc(['get_property','options/osd-level']) == 0
        assert view._ipc(['get_property','options/osd-bar']) is False
        assert view._ipc(['get_property','options/cursor-autohide']) == 'always'
        assert view._ipc(['get_property','aid']) is False or view._ipc(['get_property','aid']) in ('no', None)
        if os.environ.get('CHECK_LOOP'):
            duration = view._ipc(['get_property', 'duration'])
            assert duration and 0 < duration <= 60
            deadline = time.monotonic() + duration + 6
            previous = view._ipc(['get_property', 'time-pos']) or 0
            wrapped = False
            while time.monotonic() < deadline:
                time.sleep(.2)
                position = view._ipc(['get_property', 'time-pos'])
                if position is not None:
                    if position + 1 < previous:
                        wrapped = True
                        break
                    previous = position
            assert wrapped, 'The silent clip did not wrap within its expected period'
            print('PASS: encoded silent clip completed a loop in the actual player', flush=True)
        for attempt in range(30):
            if view._window('ambience'): break
            time.sleep(.1)
        if not view._window('ambience'):
            print('Visible classes:', [(x.get('class'),x.get('initialClass')) for x in json.loads(original_run(['hyprctl','clients','-j']).stdout)], flush=True)
            print(original_run(['journalctl','--user','-u','listening-test-ambience-'+suffix+'.service','-n','20','--no-pager']).stdout,flush=True)
        view.place('9')
        assert view._window('ambience')['workspace']['id'] == 9
        assert json.loads(original_run(['hyprctl','activeworkspace','-j']).stdout)['id'] == before
        view.place('here')
        assert view._window('ambience')['workspace']['id'] == before
        view.place('pin')
        assert view._window('ambience')['pinned']
        view.place('pin')
        assert not view._window('ambience')['pinned']
        view.place('pip')
        time.sleep(.5)
        assert view._window('ambience')['pinned']
        assert view._window('ambience')['size'] == [640, 360]
        view.place('9')
        assert not view._window('ambience')['pinned']
        view.control('toggle')
        assert view.state()['status'] == 'Paused'
        view.control('toggle')
        view.control('stop')
        assert view.state() is None
        print('PASS: offline vista, no audio, workspace9, Bring here, pin/unpin, pause/resume and close', flush=True)
        if os.environ.get('CHECK_ROOMS'):
            rooms = [item for item in view.presets() if item['id'].startswith('room-')]
            assert len(rooms) == 8
            for item in rooms:
                assert item['available']
                view.start(item, placement='9')
                assert view._ipc(['get_property','options/osc']) is False
                assert view._ipc(['get_property','options/osd-level']) == 0
                assert view._ipc(['get_property','options/osd-bar']) is False
                assert view._ipc(['get_property','options/cursor-autohide']) == 'always'
                assert view._ipc(['get_property','aid']) in (False, 'no', None)
                assert view._window('ambience')['workspace']['id'] == 9
                assert view._ipc(['get_property','duration']) >= 11.9
                view.control('stop')
            wizard = next(item for item in rooms if item['id'] == 'room-wizard-valley')
            view.start(dict(wizard, url=wizard['still_url']), placement='9')
            assert view._ipc(['get_property','options/osc']) is False
            assert view._ipc(['get_property','options/osd-level']) == 0
            assert view._ipc(['get_property','options/osd-bar']) is False
            assert view._ipc(['get_property','options/cursor-autohide']) == 'always'
            assert view._ipc(['get_property','aid']) in (False, 'no', None)
            assert view._ipc(['get_property','video-params/aspect']) > 2.3
            view.control('stop')
            assert json.loads(original_run(['hyprctl','activeworkspace','-j']).stdout)['id'] == before
            print('PASS: eight distinct silent room loops and full-composition Wizarding Valley still', flush=True)
        if os.environ.get('CHECK_YOUTUBE'):
            url = 'https://www.youtube.com/watch?v=VaLgmb5aGjo'
            view.start(url, mode='ambience', placement='9')
            assert view.state()['has_audio'] is False
            assert view._ipc(['get_property','options/osc']) is False
            assert view._ipc(['get_property','options/osd-level']) == 0
            assert view._ipc(['get_property','options/osd-bar']) is False
            assert view._ipc(['get_property','options/cursor-autohide']) == 'always'
            assert view._ipc(['get_property','aid']) in (False,'no',None)
            view.control('stop')
            print('PASS: long-form YouTube video loaded in silent mode', flush=True)
            view.start(url, mode='video', placement='9')
            assert view._ipc(['get_property','aid'],'video') not in (False,'no',None)
            view.control('volume','video',25)
            assert round(view.state('video')['volume']) == 25
            view.control('mute','video')
            assert view.state('video')['muted']
            view.control('stop','video')
            print('PASS: custom YouTube URL loaded with audio/video, separate volume and mute (null audio output)', flush=True)
    finally:
        for mode in view.MODES:
            run(['systemctl','--user','stop','listening-hub-'+mode+'.service'], check=False)
