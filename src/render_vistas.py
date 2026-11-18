"""Render eight room scenes and two legacy landscape loops during packaging."""
import math
from pathlib import Path
import random
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFilter

WIDTH, HEIGHT, FRAMES = 768, 432, 120


def render(destination, night=False):
    random.seed(2409)
    top, horizon, water = ((15, 26, 53), (71, 83, 120), (20, 43, 64)) if night else ((77, 119, 151), (196, 203, 190), (47, 93, 111))
    image = Image.new('RGB', (WIDTH, HEIGHT))
    painter = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        amount = min(y / 260, 1)
        color = tuple(round(top[i] * (1 - amount) + horizon[i] * amount) for i in range(3)) if y < 260 else water
        painter.line((0, y, WIDTH, y), fill=color)
    if night:
        for _ in range(55):
            x, y = random.randrange(WIDTH), random.randrange(165)
            painter.ellipse((x, y, x+1, y+1), fill=(171, 183, 205))
        painter.ellipse((571, 39, 606, 74), fill=(223, 220, 193))
    for layer, (base_y, amplitude, color) in enumerate([
        (215, 90, (65, 90, 113) if not night else (37, 49, 80)),
        (247, 75, (43, 72, 91) if not night else (24, 40, 64)),
        (269, 55, (29, 61, 73) if not night else (14, 32, 46)),
    ]):
        peaks = [(x, base_y - int(amplitude * (0.2 + 0.8 * random.random()))) for x in range(-60, WIDTH+90, 65)]
        painter.polygon(peaks + [(WIDTH+90, 300), (-60, 300)], fill=color)
    painter.rectangle((0, 285, WIDTH, HEIGHT), fill=water)
    # The shoreline and its blurred reflection share one geometry.
    reflection = image.crop((0, 168, WIDTH, 285)).transpose(Image.Transpose.FLIP_TOP_BOTTOM).resize((WIDTH, 140)).filter(ImageFilter.GaussianBlur(3))
    image.paste(Image.blend(Image.new('RGB', reflection.size, water), reflection, .3), (0, 285))
    encoder = subprocess.Popen(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{WIDTH}x{HEIGHT}', '-r', '12', '-i', '-', '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '25', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(destination)], stdin=subprocess.PIPE)
    try:
        for frame in range(FRAMES):
            phase = frame * 2 * math.pi / FRAMES
            shot = image.copy().convert('RGBA')
            mist = Image.new('RGBA', (WIDTH, HEIGHT))
            draw = ImageDraw.Draw(mist)
            for band in range(3):
                x = -100 + 65 * math.sin(phase + band)
                y = 254 + band * 14
                draw.ellipse((x, y, WIDTH + x + 180, y + 21), fill=(180, 199, 207, 25 if night else 35))
            shot = Image.alpha_composite(shot, mist.filter(ImageFilter.GaussianBlur(10)))
            draw = ImageDraw.Draw(shot)
            for line in range(22):
                y = 301 + line * 6
                center = (line * 137 + 25 * math.sin(phase + line)) % WIDTH
                length = 12 + line * 2
                draw.line((center-length, y, center+length, y), fill=(122, 157, 174, 255) if night else (145, 179, 183, 255))
            if frame == 0:
                shot.convert('RGB').save(destination.with_suffix('.png'))
            encoder.stdin.write(shot.convert('RGB').tobytes())
    finally:
        encoder.stdin.close()
    if encoder.wait():
        raise SystemExit('Vista encoding failed')



# Room effects are composited only inside their authored rectangles. The
# underlying plate never moves; all phases repeat exactly at the loop boundary.
from visual_scenes import SCENES

ROOM_SIZE = (1440, 810)
ROOM_FPS = 24
ROOM_FRAMES = 480
ARTWORK = Path(__file__).parent / 'artwork'


from motion_engine import prepare_scene, room_frame


def render_room(destination, scene, plate_path, size=ROOM_SIZE, frames=ROOM_FRAMES):
    with Image.open(plate_path) as plate:
        prepared = prepare_scene(plate, scene, size)
    room_frame(prepared, 0, frames).save(destination.with_suffix('.png'))
    import shutil
    still = destination.with_name(destination.stem + '-still.jpg')
    if plate_path.suffix.lower() in ('.jpg', '.jpeg'):
        shutil.copyfile(plate_path, still)
    else:
        with Image.open(plate_path) as plate:
            plate.convert('RGB').save(still, quality=92)
    encoder = subprocess.Popen([
        'ffmpeg', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
        '-s', f'{size[0]}x{size[1]}', '-r', str(ROOM_FPS), '-i', '-', '-an',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '0', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', str(destination),
    ], stdin=subprocess.PIPE)
    try:
        for frame in range(frames):
            encoder.stdin.write(room_frame(prepared, frame, frames).tobytes())
    finally:
        encoder.stdin.close()
    if encoder.wait():
        raise SystemExit('Room vista encoding failed: ' + destination.name)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--scene', choices=SCENES, help='render just one room for review')
    parser.add_argument('--frames', type=int, default=ROOM_FRAMES)
    parser.add_argument('--size', default='1440x810', help='room output size, e.g. 1440x810')
    args = parser.parse_args()
    try:
        size = tuple(map(int, args.size.lower().split('x')))
        if len(size) != 2 or any(n <= 0 or n % 2 for n in size) or args.frames <= 0:
            raise ValueError
    except ValueError:
        parser.error('--size must contain two positive even dimensions and --frames must be positive')
    scenes = {args.scene: SCENES[args.scene]} if args.scene else SCENES
    missing = [str(ARTWORK / scene['plate']) for scene in scenes.values()
               if not (ARTWORK / scene['plate']).is_file()]
    if missing:
        parser.error('required room artwork is missing: ' + ', '.join(missing))
    args.destination.mkdir(parents=True, exist_ok=True)
    if not args.scene:
        render(args.destination / 'northern-lake.mp4')
        render(args.destination / 'moonlit-lake.mp4', night=True)
    for identifier, scene in scenes.items():
        render_room(args.destination / (identifier + '.mp4'), scene,
                    ARTWORK / scene['plate'], size, args.frames)


if __name__ == '__main__':
    main()
