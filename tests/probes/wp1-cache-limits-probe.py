"""Documents the one way the snapshot file cache serves stale data: an in-place rewrite that keeps size, mtime and inode.

Not an oracle (the product never does this); it records the limit so a reviewer knows it. Prints STALE or FRESH.
"""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wp1_harness as h

with h.rig() as rig:
    path = h.STATE_DIR / 'room-variants.json'
    variant = lambda name: [{'id': 'variant-x', 'name': name, 'music': {'source_id': 'x'}}]
    h.hub.save_json(path, variant('a'))
    rig.settle_files()
    before = rig.controller.snapshot()['variants'][0]['name']
    stat = path.stat()
    with open(path, 'r+') as f:  # same inode, same size
        f.write(json.dumps(variant('b')) + '\n')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # tools such as cp -p / rsync -t / tar preserve this
    after = rig.controller.snapshot()['variants'][0]['name']
    print('STALE' if after == before else 'FRESH', before, after)
