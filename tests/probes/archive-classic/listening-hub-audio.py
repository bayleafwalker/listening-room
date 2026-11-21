"""Decode catalogue streams in isolated, silent player units.

Run from a graphical session. Set CHECK_YOUTUBE=1 to test public-video audio.
No normal Listening Hub unit, state, cookie jar or audio output is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

package = Path(sys.argv[1])
source = Path(sys.argv[2]) if len(sys.argv) > 2 else package / 'share/listening-hub'
wrapper = '\n'.join(p.read_bytes().decode('utf-8', errors='ignore') for p in (package/'bin').iterdir() if p.name.startswith('.listening-hub'))
bins = re.findall(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._-]+/bin', wrapper)
os.environ['PATH'] = ':'.join(dict.fromkeys(bins)) + ':' + os.environ['PATH']
sys.path.insert(0,str(source))
os.environ['LISTENING_HUB_CATALOG'] = str(source/'catalogue.json')
import listening_hub as hub
import player_core as core
real_run = core.run
identity = 'listening-audio-probe-'+str(os.getpid())
def run(args, **kwargs):
    return real_run([arg.replace('listening-hub-radio',identity) for arg in args], **kwargs)
core.run = run

with tempfile.TemporaryDirectory(prefix='listening-audio-probe-') as directory:
    work = Path(directory)
    core.STATE = work/'state'
    core.RUNTIME = work/'runtime'
    core.RUNTIME.mkdir(mode=0o700)
    core.SOCKET = core.RUNTIME/'radio.sock'
    mpv = shutil.which('mpv')
    assert mpv
    shim = work/'mpv'
    shim.write_text('#!'+shutil.which('python3')+'\nimport os,sys\nos.execv('+repr(mpv)+',['+repr(mpv)+',"--ao=null"]+sys.argv[1:])\n')
    shim.chmod(0o700)
    os.environ['PATH'] = str(work)+':'+os.environ['PATH']
    def decoded():
        end=time.monotonic()+8
        while time.monotonic()<end:
            try:
                rate=hub.ipc(['get_property','audio-params/samplerate'])
                if rate and rate>0:return
            except (hub.HubError,OSError):pass
            time.sleep(.2)
        raise AssertionError('No decoded audio samples')
    try:
        for source_id in ('ylex','yle-klassinen','radio-dismuke'):
            item=next(x for x in hub.sources() if x['id']==source_id)
            assert item['kind']=='radio'
            hub.play(item)
            decoded()
            assert hub.ipc(['get_property','vid']) in (False,None,'no')
            run(['systemctl','--user','stop','listening-hub-radio.service'])
            print('PASS: direct stream decoded with null audio output:',source_id,flush=True)
        if os.environ.get('CHECK_YOUTUBE'):
            core.start_radio({'id': 'youtube-video', 'name': 'YouTube audio', 'url': core.youtube_video_url('https://www.youtube.com/watch?v=VaLgmb5aGjo')}, ytdlp=shutil.which('yt-dlp'))
            decoded()
            assert hub.ipc(['get_property','vid']) in (False,None,'no')
            print('PASS: public YouTube video decoded as audio without browser/account access',flush=True)
    finally:
        run(['systemctl','--user','stop','listening-hub-radio.service'],check=False)
