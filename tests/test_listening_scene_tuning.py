"""Scene tuning is validated, every parameter is used, and cloud drift is even."""
import copy
import json
from pathlib import Path
import sys
import unittest

from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import visual_scenes
from motion_engine import prepare_scene, room_frame
from visual_scenes import SCENES, validate_tuning

SRC = Path(visual_scenes.__file__).parent
TUNING = {p.name: json.loads(p.read_text()) for p in sorted((SRC / 'scene_tuning').glob('*.json'))}


def broken(change):
    tuning = copy.deepcopy(TUNING['midnight-library.json'])
    change(tuning)
    return tuning


def fire(tuning):
    return next(e for e in tuning['effects'] if e['kind'] == 'fire')


class TuningValidationTest(unittest.TestCase):
    def test_every_shipped_tuning_file_is_valid(self):
        self.assertTrue(TUNING)
        for name, tuning in TUNING.items():
            validate_tuning(name, tuning)

    def rejects(self, change, *fragments):
        with self.assertRaises(ValueError) as caught:
            validate_tuning('midnight-library.json', broken(change))
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_a_typo_in_a_parameter_name_fails_with_a_clear_message(self):
        self.rejects(lambda t: fire(t).update(hete=2), 'midnight-library.json', 'effect', '(fire)',
                     "unknown parameter 'hete'", 'heat')

    def test_the_removed_lick_key_is_rejected(self):
        self.rejects(lambda t: fire(t).update(lick=.9), "unknown parameter 'lick'")

    def test_a_parameter_of_another_effect_is_rejected(self):
        self.rejects(lambda t: fire(t).update(rain_cycles=[2]), "unknown parameter 'rain_cycles'")

    def test_out_of_range_and_wrong_type_values_fail_naming_the_parameter(self):
        self.rejects(lambda t: fire(t).update(heat=60), 'heat must be a number from 0.1 to 6')
        self.rejects(lambda t: fire(t).update(bed=-.1), 'bed must be')
        self.rejects(lambda t: fire(t).update(rise='fast'), 'rise must be a whole number')
        self.rejects(lambda t: fire(t).update(rise=True), 'rise must be a whole number')
        self.rejects(lambda t: fire(t).update(fill_gap=8), 'fill_gap must be an odd')
        self.rejects(lambda t: fire(t).update(strength=1.5), 'strength must be')
        self.rejects(lambda t: t['effects'][0].update(color=[1, 2, 300]), 'color must be three whole numbers')
        self.rejects(lambda t: t['effects'][0].update(rect=[.5, .5, .4, .6]), 'rect must be four numbers')
        self.rejects(lambda t: t['effects'][0].update(polygon=[[0, 0], [1, 1]]), 'polygon must be a polygon')

    def test_unknown_kinds_top_level_keys_and_rigid_parameters_fail(self):
        self.rejects(lambda t: fire(t).update(kind='flame'), "unknown kind 'flame'")
        self.rejects(lambda t: t.update(effect=[]), "unknown key 'effect'")
        self.rejects(lambda t: t['rigid'][0].update(grown=2), 'rigid region 1', "unknown parameter 'grown'")
        self.rejects(lambda t: t['rigid'][0].update(grow=9), 'grow must be')

    @unittest.skip('ORIGINAL, superseded by test_a_bad_shipped_file_stops_the_import (R-src1, acceptance author, WP4): '
                   'it looked for one call spelled exactly so in visual_scenes.py instead of importing a broken tree')
    def test_loading_a_bad_file_stops_the_import(self):
        # The loader calls validate_tuning for every file, so a typo in a
        # shipped file cannot reach a build: importing visual_scenes fails.
        source = (SRC / 'visual_scenes.py').read_text()
        self.assertIn('validate_tuning(_path.name, _tuning)', source)

    def test_a_bad_shipped_file_stops_the_import(self):
        # A typo in any shipped tuning file cannot reach a build: importing visual_scenes from that tree fails and
        # names the file and the key. The clean copy is the control.
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            for name in sorted(TUNING):
                tree = Path(folder) / name
                tree.mkdir()
                shutil.copy(SRC / 'visual_scenes.py', tree)
                shutil.copytree(SRC / 'scene_tuning', tree / 'scene_tuning')
                load = lambda: subprocess.run([sys.executable, '-c', 'import visual_scenes'], cwd=tree, capture_output=True, text=True,
                                              timeout=60, stdin=subprocess.DEVNULL, env={'PATH': '', 'PYTHONPATH': str(tree)})
                clean = load()
                self.assertEqual(clean.returncode, 0, clean.stderr[-400:])
                tuning = copy.deepcopy(TUNING[name])
                tuning['effects'][0]['sizze'] = 1
                (tree / 'scene_tuning' / name).write_text(json.dumps(tuning))
                result = load()
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn(name, result.stderr)
                self.assertIn('sizze', result.stderr)

    def test_every_documented_parameter_is_read_by_the_engine(self):
        engine = (SRC / 'motion_engine.py').read_text()
        names = set(visual_scenes._COMMON) | (set(visual_scenes._RIGID) - {'name'})
        for table in visual_scenes._PARAMETERS.values():
            names |= set(table)
        unused = sorted(name for name in names if "'%s'" % name not in engine)
        self.assertEqual(unused, [])

    def test_shipped_files_use_only_documented_parameters(self):
        # Complements validation: the schema lists exactly what the engine reads.
        for name, tuning in TUNING.items():
            for effect in tuning['effects']:
                allowed = set(visual_scenes._COMMON) | set(visual_scenes._PARAMETERS[effect['kind']])
                self.assertLessEqual(set(effect), allowed, name)


class CloudDriftTest(unittest.TestCase):
    def test_cloud_drift_has_no_periodic_tick(self):
        # Every frame step must be about as big as its neighbours: a tick shows
        # as one step far larger than the typical one.
        for room in ('room-after-hours', 'room-atomic-lounge', 'room-wizard-valley'):
            scene = copy.deepcopy(SCENES[room])
            scene['effects'] = [e for e in scene['effects'] if e['kind'] == 'cloud']
            with Image.open(SRC / 'artwork' / scene['plate']) as plate:
                prepared = prepare_scene(plate, scene, (480, 270))
            frames = 120
            shots = [room_frame(prepared, n, frames) for n in range(frames)] + [room_frame(prepared, 0, frames)]
            steps = sorted(ImageStat.Stat(ImageChops.difference(a, b)).mean[0] for a, b in zip(shots, shots[1:]))
            median = steps[len(steps)//2]
            self.assertGreater(median, 0, room)
            self.assertLess(steps[-1], 3*median, '%s: one step is %.3f against a median of %.3f' % (
                room, steps[-1], median))
            plain = prepared[0]
            self.assertEqual(ImageChops.difference(shots[0], plain).getbbox() is None
                             or ImageStat.Stat(ImageChops.difference(shots[0], plain)).mean[0] < .01, True, room)


if __name__ == '__main__':
    unittest.main()
