#!/usr/bin/env python3
"""Pick the README screenshots from the probe output: curate-screenshots.py RAW OUT.

RAW holds the output of listening-session-ui.py (session/) and
listening-listen-ui.py (listen/). Both use fixtures only. Every image is
re-saved as an optimised PNG; the selection and names are fixed here so the
README never points at a missing file.
"""
import sys
from pathlib import Path
from PIL import Image

PICKS = [
    ('01-now-playing', 'session/05-now-radio'),
    ('02-rooms', 'session/03-rooms'),
    ('03-room-preview', 'session/04-room-preview'),
    ('04-listen', 'listen/01-listen-shelves'),
    ('05-station-detail', 'session/11-station-detail'),
    ('06-mix', 'session/07-mix'),
    ('07-preferences', 'session/13-connections'),
    ('08-mini-player', 'session/14-mini-player'),
    ('09-waybar-popover', 'session/16-waybar-popover'),
    ('10-narrow', 'session/17-mini-layout-420'),
]

raw, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
for name, source in PICKS:
    Image.open(raw / (source + '.png')).convert('RGB').save(out / (name + '.png'), optimize=True)
    print(name)
