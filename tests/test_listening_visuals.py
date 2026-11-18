"""Contract tests for bounded and repeatable offline room visuals."""
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from render_vistas import prepare_scene, room_frame
from visual_scenes import SCENES
import visual_scenes


class RoomVisualsTest(unittest.TestCase):
    def test_scene_coverage_and_unique_plates(self):
        self.assertEqual(set(SCENES), {'room-' + slug for slug in (
            'midnight-library', 'night-drive', 'northern-quiet', 'velvet-circuit',
            'after-hours', 'atomic-lounge', 'finnish-discoveries', 'wizard-valley')})
        self.assertEqual(len({s['plate'] for s in SCENES.values()}), len(SCENES))
        for scene in SCENES.values():
            self.assertTrue(all(scene[key] for key in (
                'title', 'accent', 'description', 'motion', 'audio_tone', 'plate', 'effects')))
        tuning = Path(visual_scenes.__file__).parent / 'scene_tuning'
        self.assertEqual({p.stem for p in tuning.glob('*.json')}, {key.removeprefix('room-') for key in SCENES})

    def test_wide_plate_keeps_both_edges(self):
        plate = Image.new('RGB', (400, 100), (30, 50, 70))
        for x in range(30):
            for y in range(100):
                plate.putpixel((x, y), (240, 20, 20))
                plate.putpixel((399 - x, y), (20, 240, 20))
        base, _ = prepare_scene(plate, SCENES['room-wizard-valley'], (320, 180))
        self.assertEqual(base.getpixel((5, 90)), (240, 20, 20))
        self.assertEqual(base.getpixel((314, 90)), (20, 240, 20))
        self.assertEqual(base.getpixel((160, 0)), (0, 0, 0))

    def test_loop_is_repeatable_and_preserves_pixels_outside_effects(self):
        plate = Image.new('RGB', (320, 180), (30, 50, 70))
        # Source-warp effects correctly leave a uniform plate unchanged. Give
        # them painted detail to move while checking the same spatial bounds.
        pixels = plate.load()
        for y in range(180):
            for x in range(320):
                pixels[x, y] = ((x * 7 + y * 3) % 256,
                                (x * 3 + y * 5) % 256,
                                (x * 5 + y * 7) % 256)
        for identifier, scene in SCENES.items():
            with self.subTest(scene=identifier):
                prepared = prepare_scene(plate, scene, plate.size)
                first = room_frame(prepared, 0, 144)
                self.assertIsNone(ImageChops.difference(first, room_frame(prepared, 144, 144)).getbbox())
                self.assertIsNone(ImageChops.difference(first, room_frame(prepared, 0, 144)).getbbox())
                samples = [room_frame(prepared, frame, 144) for frame in (18, 36, 54, 72, 90, 108, 126)]
                self.assertTrue(any(ImageChops.difference(first, sample).getbbox() for sample in samples), 'The effect must produce visible pixel changes during the loop')
                other = samples[1]
                for x, y in ((0, 0), (319, 0), (0, 179), (319, 179)):
                    self.assertEqual(other.getpixel((x, y)), plate.getpixel((x, y)))
                for y in range(180):
                    for x in range(320):
                        inside = any(round(r[0] * 320) <= x < round(r[2] * 320) and round(r[1] * 180) <= y < round(r[3] * 180)
                                     for r in (effect['rect'] for effect in scene['effects']))
                        if not inside:
                            self.assertEqual(other.getpixel((x, y)), plate.getpixel((x, y)))

    def test_polygon_and_protected_architecture_do_not_receive_rain(self):
        plate = Image.new('RGB', (200, 200), (30, 50, 70))
        scene = {'effects': [{'kind': 'rain', 'rect': [0, 0, 1, 1],
            'polygon': [[0, 0], [.5, 0], [.5, 1], [0, 1]],
            'protect': [[[.1, .1], [.4, .1], [.4, .4], [.1, .4]]],
            'count': 200, 'strength': 1, 'seed': 9}]}
        prepared = prepare_scene(plate, scene, plate.size)
        changed = False
        for frame in (0, 18, 36, 54, 72):
            result = room_frame(prepared, frame, 144)
            self.assertIsNone(ImageChops.difference(result.crop((101,0,200,200)), plate.crop((101,0,200,200))).getbbox())
            self.assertIsNone(ImageChops.difference(result.crop((21,21,79,79)), plate.crop((21,21,79,79))).getbbox())
            changed |= ImageChops.difference(result, plate).getbbox() is not None
        self.assertTrue(changed)

    def test_rain_falls_in_one_direction_between_wraps(self):
        plate = Image.new('RGB', (200, 200), (10, 20, 30))
        prepared = prepare_scene(plate, {'effects':[{'kind':'rain','rect':[0,0,1,1],'count':1,'cycles':1,'strength':1}]}, plate.size)
        prepared[1][0]['particles'] = [(.5,.1,.5,1)]
        positions = [ImageChops.difference(room_frame(prepared, frame, 480), plate).getbbox()[1] for frame in (120,240,360)]
        self.assertLess(positions[0], positions[1])
        self.assertLess(positions[1], positions[2])

    def test_optional_rain_speeds_are_downward_and_periodic(self):
        plate = Image.new('RGB', (200, 200), (10, 20, 30))
        scene = {'effects': [{'kind': 'rain', 'rect': [0, 0, 1, 1],
                              'strength': 1, 'rain_cycles': [1, 2]}]}
        prepared = prepare_scene(plate, scene, plate.size)
        prepared[1][0]['particles'] = [(.3, .1, .1, 1), (.7, .1, .9, 1)]
        first = room_frame(prepared, 0)
        later = room_frame(prepared, 48)
        self.assertIsNone(ImageChops.difference(first, room_frame(prepared, 480)).getbbox())
        difference = ImageChops.difference(later, plate)
        slow = difference.crop((40, 0, 80, 200)).getbbox()[1]
        fast = difference.crop((120, 0, 160, 200)).getbbox()[1]
        self.assertLess(slow, fast)
        self.assertGreater(slow, 20)
        prepared[1][0]['rain_cycles'] = [0]
        with self.assertRaises(ValueError):
            room_frame(prepared, 0)

    def test_midnight_library_does_not_repeat_at_legacy_short_period(self):
        plate = Image.new('RGB', (320, 180), (30, 50, 70))
        prepared = prepare_scene(plate, SCENES['room-midnight-library'], plate.size)
        first = room_frame(prepared, 0)
        for frame in (160, 320):
            self.assertIsNotNone(ImageChops.difference(first, room_frame(prepared, frame)).getbbox())
        self.assertIsNone(ImageChops.difference(first, room_frame(prepared, 480)).getbbox())

    def test_fire_and_electrical_are_periodic_and_respect_protection(self):
        plate = Image.new('RGB', (160, 120))
        for y in range(120):
            for x in range(160):
                plate.putpixel((x, y), (100 + x % 100, 70 + y % 100, 40))
        for kind in ('fire', 'electrical'):
            effect = {'kind': kind, 'rect': [.1, .1, .9, .9], 'strength': 1,
                      'seed': 123, 'protect': [[[.45,.1],[.55,.1],[.55,.9],[.45,.9]]],
                      'threshold': 20}
            prepared = prepare_scene(plate, {'effects': [effect]}, plate.size)
            frames = [room_frame(prepared, n) for n in (0, 60, 120, 240, 480)]
            self.assertIsNone(ImageChops.difference(frames[0], frames[-1]).getbbox())
            self.assertTrue(any(ImageChops.difference(frames[0], f).getbbox() for f in frames[1:-1]))
            for f in frames:
                self.assertIsNone(ImageChops.difference(f.crop((74, 15, 86, 105)), plate.crop((74, 15, 86, 105))).getbbox())
                self.assertEqual(f.getpixel((0, 0)), plate.getpixel((0, 0)))

    def test_electrical_seed_produces_distinct_patterns(self):
        plate = Image.new('RGB', (100, 100), (190, 150, 120))
        def frames(seed):
            scene = {'effects': [{'kind': 'electrical', 'rect': [0,0,1,1],
                                  'seed': seed, 'threshold': 30}]}
            prepared = prepare_scene(plate, scene, plate.size)
            return [room_frame(prepared, n).getpixel((50,50)) for n in (0,60,120,180)]
        self.assertNotEqual(frames(1), frames(2))
        self.assertEqual(frames(1), frames(1))

    def test_water_moves_existing_reflections_and_preserves_surroundings(self):
        plate = Image.new('RGB', (160, 120), (40,60,80))
        for x in range(50,110,8):
            for y in range(30,90): plate.putpixel((x,y), (170,180,190))
        prepared = prepare_scene(plate, {'effects':[{'kind':'water','rect':[.25,.2,.75,.8],'strength':1}]}, plate.size)
        frames = [room_frame(prepared, frame, 480) for frame in (0,60,120)]
        self.assertTrue(any(ImageChops.difference(frames[0], f).getbbox() for f in frames[1:]))
        for f in frames:
            self.assertEqual(f.getpixel((0,0)),plate.getpixel((0,0)))
            self.assertTrue(all(low >= minimum for (low, _), minimum in zip(f.getextrema(), (40,60,80))))


if __name__ == '__main__':
    unittest.main()
