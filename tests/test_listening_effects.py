"""Spatial room motion stays deterministic, masked and exactly periodic."""
import sys
from pathlib import Path
import unittest

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from motion_engine import prepare_scene, room_frame


class RoomSpatialMotionTests(unittest.TestCase):
    def setUp(self):
        self.plate = Image.new('RGB', (144, 81))
        px = self.plate.load()
        for y in range(81):
            for x in range(144):
                px[x, y] = ((x * 19 + y * 7) % 256, (x * 11 + y * 17) % 256, (x * 3 + y * 23) % 256)

    def effect(self, kind):
        return dict(kind=kind, rect=[.2, .2, .8, .8], polygon=[[.2,.2],[.8,.2],[.8,.8],[.2,.8]],
                    protect=[[[.45,.4],[.55,.4],[.55,.6],[.45,.6]]], strength=1, cycles=2, seed=8,
                    amplitude=20, feather=.01)

    def test_period_mask_and_motion(self):
        for kind in ('cloud', 'smoke', 'water', 'shimmer'):
            with self.subTest(kind=kind):
                prepared = prepare_scene(self.plate, {'effects':[self.effect(kind)]}, self.plate.size)
                first = room_frame(prepared, 0, 480)
                phase = room_frame(prepared, 73, 480)
                self.assertIsNone(ImageChops.difference(first, room_frame(prepared, 480, 480)).getbbox())
                self.assertIsNone(ImageChops.difference(phase, room_frame(prepared, 73, 480)).getbbox())
                self.assertIsNotNone(ImageChops.difference(first, phase).getbbox())
                self.assertEqual(phase.getpixel((5,5)), self.plate.getpixel((5,5)))
                self.assertEqual(phase.getpixel((72,40)), self.plate.getpixel((72,40)))

    def test_invalid_spatial_amplitude_is_rejected(self):
        for amplitude in (-1, 31, float('nan')):
            for kind in ('cloud', 'smoke', 'water'):
                effect = self.effect(kind)
                effect['amplitude'] = amplitude
                with self.subTest(kind=kind, amplitude=amplitude), self.assertRaises(ValueError):
                    room_frame(prepare_scene(self.plate, {'effects':[effect]}, self.plate.size), 0)


if __name__ == '__main__':
    unittest.main()
