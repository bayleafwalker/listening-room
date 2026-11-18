#!/usr/bin/env python3
"""Review rendered room loops by eye: contact sheets, effect close-ups, rigid
masks, motion maps, rigid-stability numbers and short preview clips.

  nix develop -c python3 scripts/scene-review.py ASSETS OUTDIR \
      [--baseline OLD_ASSETS] [--scene ROOM ...] [--no-preview]

ASSETS holds room-*.mp4 deliveries (for example the output of
`nix build .#listening-hub.assets`). With --baseline, each close-up strip
stacks the old render above the new one at the same frames. Nothing is played
and nothing outside OUTDIR is written.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PIL import Image, ImageChops, ImageDraw, ImageOps  # noqa: E402
from motion_engine import rigid_mask  # noqa: E402
from validate_vistas import reference_plate, rigid_stability  # noqa: E402
from visual_scenes import SCENES  # noqa: E402

SIZE = (1440, 810)
SHEET_FRAMES = (0, 60, 120, 180, 240, 300, 360, 420)
CLOSE_FRAMES = (0, 3, 6, 9, 120, 240, 479)


def decode(video, frames):
    selection = '+'.join('eq(n\\,%d)' % n for n in frames)
    blob = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', 'select=' + selection,
                                    '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    length = SIZE[0] * SIZE[1] * 3
    return dict(zip(frames, (Image.frombytes('RGB', SIZE, blob[i:i+length])
                             for i in range(0, len(blob), length))))


def labelled_grid(tiles, columns, label_height=16):
    width, height = tiles[0][1].size
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new('RGB', (columns*(width+4), rows*(height+label_height+4)), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    for index, (label, tile) in enumerate(tiles):
        x, y = index % columns * (width+4), index // columns * (height+label_height+4)
        sheet.paste(tile, (x, y+label_height))
        draw.text((x+3, y+2), label, fill=(230, 230, 230))
    return sheet


def close_up_box(rect, margin=12):
    x0, y0, x1, y1 = (round(v * SIZE[i % 2]) for i, v in enumerate(rect))
    return (max(0, x0-margin), max(0, y0-margin), min(SIZE[0], x1+margin), min(SIZE[1], y1+margin))


def strip(frames, box, scale):
    size = (round((box[2]-box[0])*scale), round((box[3]-box[1])*scale))
    resample = Image.Resampling.NEAREST if scale >= 2 else Image.Resampling.LANCZOS
    return [('f%d' % n, frames[n].crop(box).resize(size, resample)) for n in CLOSE_FRAMES]


def review(identity, scene, assets, out, baseline=None, preview=True):
    slug = identity.removeprefix('room-')
    video = assets / (identity + '.mp4')
    frames = decode(video, sorted(set(SHEET_FRAMES + CLOSE_FRAMES)))
    old = decode(baseline / (identity + '.mp4'), CLOSE_FRAMES) if baseline else None
    labelled_grid([('f%d' % n, frames[n].resize((360, 202), Image.Resampling.LANCZOS))
                   for n in SHEET_FRAMES], 4).save(out / (slug + '-sheet.jpg'), quality=88)
    # One close-up per effect group: overlapping rectangles share a strip.
    boxes = []
    for effect in scene['effects']:
        box = close_up_box(effect['rect'])
        for i, other in enumerate(boxes):
            if box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]:
                boxes[i] = (min(box[0], other[0]), min(box[1], other[1]),
                            max(box[2], other[2]), max(box[3], other[3]))
                break
        else:
            boxes.append(box)
    for index, box in enumerate(boxes, 1):
        scale = min(4, max(.5, 300 / max(box[2]-box[0], box[3]-box[1])))
        tiles = strip(frames, box, scale)
        if old:
            tiles = [('before ' + label, tile) for label, tile in strip(old, box, scale)] + \
                    [('after ' + label, tile) for label, tile in tiles]
        labelled_grid(tiles, len(CLOSE_FRAMES)).save(out / ('%s-closeup-%d.png' % (slug, index)))
        if preview:
            # Four seconds of the close-up at 12 fps, the long side 480 px.
            w, h = box[2]-box[0], box[3]-box[1]
            factor = 480 / max(w, h)
            subprocess.check_call(['ffmpeg', '-v', 'error', '-y', '-t', '4', '-i', str(video), '-vf',
                                   'crop=%d:%d:%d:%d,fps=12,scale=%d:%d:flags=lanczos' % (
                                       w, h, box[0], box[1], round(w*factor/2)*2, round(h*factor/2)*2),
                                   '-c:v', 'libwebp', '-q:v', '60', '-loop', '0',
                                   str(out / ('%s-closeup-%d.webp' % (slug, index)))])
    # Rigid regions in cyan, effect rectangles in yellow.
    with Image.open(reference_plate(identity, scene, assets)) as plate:
        base = ImageOps.pad(plate.convert('RGB'), SIZE, method=Image.Resampling.LANCZOS, color=(0, 0, 0))
    overlay = base.point(lambda v: v * 3 // 5)
    mask = rigid_mask(scene, base)
    if mask is not None:
        overlay = Image.composite(Image.blend(overlay, Image.new('RGB', SIZE, (0, 230, 255)), .55), overlay, mask)
    draw = ImageDraw.Draw(overlay)
    for box in (close_up_box(effect['rect'], 0) for effect in scene['effects']):
        draw.rectangle(box, outline=(255, 220, 0))
    overlay.save(out / (slug + '-rigid.jpg'), quality=88)
    # Where pixels change at all across the loop (every eighth frame).
    blob = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', 'select=not(mod(n\\,8))',
                                    '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
    length = SIZE[0] * SIZE[1]
    lumas = [Image.frombytes('L', SIZE, blob[i:i+length]) for i in range(0, len(blob), length)]
    changed = Image.new('L', SIZE)
    for luma in lumas[1:]:
        changed = ImageChops.lighter(changed, ImageChops.difference(lumas[0], luma))
    heat = changed.point(lambda v: 0 if v < 3 else min(255, 90 + v*6))
    Image.composite(Image.new('RGB', SIZE, (255, 50, 40)), base.point(lambda v: v // 2), heat).save(
        out / (slug + '-motion.jpg'), quality=85)
    if preview:
        # Six seconds across the loop boundary (17 s to 3 s), so a seam shows.
        subprocess.check_call(['ffmpeg', '-v', 'error', '-y', '-stream_loop', '1', '-ss', '17', '-t', '6',
                               '-i', str(video), '-vf', 'fps=12,scale=640:-2', '-c:v', 'libwebp',
                               '-q:v', '50', '-loop', '0', str(out / (slug + '-preview.webp'))])
    return rigid_stability(video, scene, reference_plate(identity, scene, assets), list(range(0, 480, 20)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('assets', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--scene', action='append', choices=[k.removeprefix('room-') for k in SCENES])
    parser.add_argument('--no-preview', dest='preview', action='store_false')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {}
    for identity, scene in SCENES.items():
        if args.scene and identity.removeprefix('room-') not in args.scene:
            continue
        report[identity] = review(identity, scene, args.assets, args.out, args.baseline, args.preview)
        print(json.dumps({identity: report[identity]}), flush=True)
    (args.out / 'rigid-stability.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
