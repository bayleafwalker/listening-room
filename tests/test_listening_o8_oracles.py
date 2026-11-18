"""Independent acceptance oracles for O8 (scene visuals), run on built room assets.

Owned by the acceptance author. This is a focused check, not part of the package
checkPhase: it decodes every room video, so it needs the built assets and ffmpeg.

    scripts/listening-dev.sh test test_listening_o8_oracles.py

It skips when LISTENING_HUB_ASSETS does not hold room videos or numpy is absent.

The rigid oracle deliberately ignores the scene's own ``rigid`` list. It looks for
geometric motion anywhere outside the regions whose subject genuinely flows
(water, cloud): in each block with strong edges, a frame that matches a
one- or two-pixel shifted copy of the painting better than the painting itself
has moved.
Edges and the reference come from the painting itself, so drawn matter never
counts as structure. Brightness-only effects (lights, glow, candle, shimmer) keep
the unshifted painting the best match, and overlays (rain, mist, smoke wisps,
procedural flame) do not favour a shift.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))
from visual_scenes import SCENES  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover - the dev shell provides numpy
    np = None

ASSETS = Path(os.environ.get('LISTENING_HUB_ASSETS', ''))
W, H, FPS, FRAMES = 1440, 810, 24, 480
FLOW = {'water', 'cloud'}
LIGHT = {'fire', 'candle', 'electrical', 'glow', 'shimmer'}
BLOCK = 24
SHIFTS = ((0, 1), (0, -1), (1, 0), (-1, 0), (0, 2), (0, -2), (2, 0), (-2, 0))
# A shifted painting may explain a moved frame at most this much better (correlation).
SHIFT_LIMIT = .05
FLICKER_HZ = 2.0


def available():
    return (np is not None and shutil.which('ffmpeg') is not None
            and all((ASSETS / (identity + '.mp4')).is_file() for identity in SCENES))


def decode(identity):
    """Every frame of a room video as grey levels, shape (frames, H, W)."""
    blob = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(ASSETS / (identity + '.mp4')),
                                    '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
    return np.frombuffer(blob, np.uint8).reshape(-1, H, W)


def pixel_rect(rect):
    return [round(v * (W if i % 2 == 0 else H)) for i, v in enumerate(rect)]


def correlation(a, b):
    a = a - a.mean()
    b = b - b.mean()
    scale = np.sqrt((a * a).sum() * (b * b).sum())
    return 1.0 if scale < 1e-9 else float((a * b).sum() / scale)


def painting(scene):
    """The authored plate as grey levels at the render size, as the renderer pads it."""
    from PIL import Image, ImageOps
    with Image.open(SOURCE / 'artwork' / scene['plate']) as plate:
        base = ImageOps.pad(plate.convert('RGB'), (W, H), method=Image.Resampling.LANCZOS, color=(0, 0, 0))
    return np.asarray(base.convert('L'), dtype=float)


def moved_blocks(frames, scene):
    """Painted-edge blocks outside flowing subjects whose content is displaced.

    The reference is the painting itself, not frame 0, so matter an effect draws
    (rain, flames, sparks) never counts as structure.
    """
    samples = frames[::12].astype(float)
    first = painting(scene)
    spread = samples.max(0) - samples.min(0)
    gradient = np.zeros_like(first)
    gradient[:, 1:-1] = np.abs(first[:, 2:] - first[:, :-2])
    gradient[1:-1, :] = np.maximum(gradient[1:-1, :], np.abs(first[2:, :] - first[:-2, :]))
    edge = gradient >= 24
    flow = np.zeros((H, W), bool)
    for effect in scene['effects']:
        if effect['kind'] in FLOW:
            x0, y0, x1, y1 = pixel_rect(effect['rect'])
            flow[y0:y1, x0:x1] = True
    moved = []
    for by in range(2, H - BLOCK - 2, BLOCK):
        for bx in range(2, W - BLOCK - 2, BLOCK):
            box = (slice(by, by + BLOCK), slice(bx, bx + BLOCK))
            edges = edge[box] & ~flow[box]
            if flow[box].mean() > .5 or edges.sum() < BLOCK or (~edges & ~flow[box]).sum() < BLOCK:
                continue
            # Cheap prefilter: change concentrated on edges, not spread evenly.
            on_edge, off_edge = spread[box][edges].mean(), spread[box][~edges & ~flow[box]].mean()
            if on_edge < 8 or on_edge < 2.5 * off_edge + 2:
                continue
            # Judge only the block's pixels outside the flowing subject.
            keep = ~flow[box]
            worst = 0.
            for frame in samples:
                still = correlation(frame[box][keep], first[box][keep])
                shifted = max(correlation(frame[box][keep],
                                          first[by + dy:by + dy + BLOCK, bx + dx:bx + dx + BLOCK][keep])
                              for dy, dx in SHIFTS)
                worst = max(worst, shifted - still)
            if worst > SHIFT_LIMIT:
                moved.append((bx, by, round(worst, 3)))
    return moved


@unittest.skipUnless(available(), 'needs built room assets (LISTENING_HUB_ASSETS), ffmpeg and numpy')
class SceneOracles(unittest.TestCase):
    frames = {}

    @classmethod
    def video(cls, identity):
        if identity not in cls.frames:
            cls.frames.clear()  # one room in memory at a time
            cls.frames[identity] = decode(identity)
        return cls.frames[identity]

    def test_rooms_loop_480_frames_at_24_fps(self):
        for identity in SCENES:
            with self.subTest(identity):
                self.assertEqual(len(self.video(identity)), FRAMES)

    def test_no_rigid_structure_moves_outside_flowing_subjects(self):
        for identity, scene in SCENES.items():
            with self.subTest(identity):
                self.assertEqual(moved_blocks(self.video(identity), scene), [],
                                 identity + ': displaced edge blocks (x, y, correlation gain)')

    def test_loop_seam_is_no_bigger_than_an_ordinary_step(self):
        for identity in SCENES:
            with self.subTest(identity):
                frames = self.video(identity)
                steps = [float(np.abs(frames[i + 1].astype(np.int16) - frames[i]).mean())
                         for i in range(len(frames) - 1)]
                seam = float(np.abs(frames[0].astype(np.int16) - frames[-1]).mean())
                # The wrap must look like any other frame step: no larger than the
                # largest step inside the loop (with encode slack) and visually tiny.
                self.assertLessEqual(seam, max(steps) * 1.25 + .01, identity)
                self.assertLess(seam, .25, identity)

    def test_light_flicker_stays_slow(self):
        frequencies = np.fft.rfftfreq(FRAMES, 1 / FPS)
        for identity, scene in SCENES.items():
            frames = self.video(identity)
            for index, effect in enumerate(scene['effects']):
                if effect['kind'] not in LIGHT:
                    continue
                with self.subTest(identity, effect=index, kind=effect['kind']):
                    x0, y0, x1, y1 = pixel_rect(effect['rect'])
                    series = frames[:, y0:y1, x0:x1].mean(axis=(1, 2))
                    power = np.abs(np.fft.rfft(series - series.mean()))[1:] ** 2
                    if power.sum() < 1e-6:
                        continue
                    self.assertLessEqual(frequencies[1:][power.argmax()], FLICKER_HZ, 'dominant flicker')
                    self.assertLess(power[frequencies[1:] > FLICKER_HZ].sum() / power.sum(), .25,
                                    'flicker energy above 2 Hz')

    def test_reduced_motion_still_is_the_unaltered_painting(self):
        from PIL import Image, ImageChops, ImageStat
        for identity, scene in SCENES.items():
            with self.subTest(identity):
                still = ASSETS / (identity + '-still.jpg')
                plate = SOURCE / 'artwork' / scene['plate']
                self.assertTrue(still.is_file())
                if plate.suffix.lower() in ('.jpg', '.jpeg'):
                    self.assertEqual(still.read_bytes(), plate.read_bytes(), 'still must be the plate itself')
                else:
                    with Image.open(still) as a, Image.open(plate) as b:
                        self.assertEqual(a.size, b.size)
                        difference = ImageStat.Stat(ImageChops.difference(a.convert('RGB'), b.convert('RGB'))).mean
                        self.assertLess(max(difference), 2.0, 'still must be the plate, re-encoded only')


if __name__ == '__main__':
    unittest.main()
