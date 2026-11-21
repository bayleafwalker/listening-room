"""Independent acceptance oracles for O9-SCENES (scene polish after O8).

Owned by the acceptance author; not part of the package checkPhase because they decode
built room videos, need ffmpeg and numpy, and compare against the pre-O9 build.

    LISTENING_HUB_ASSETS=<assets of the change> \
    LISTENING_O9_BASELINE_ASSETS=<assets built from the pre-O9 commit> \
    python3 tests/test_listening_o9_scenes_oracles.py

The baseline assets come from the commit before O9 (0750cc4):
    nix build --no-link --print-out-paths "git+file://$PWD?rev=<sha>#listening-hub.assets"
The baseline source for the tuning mutation controls comes from `git archive` of the same
commit (LISTENING_O9_BASELINE_REV, default 0750cc4bf7ca1a0f11d2bb72bff97eacab65934a).
Checks that need a baseline skip without one; every sharp check also asserts that the
baseline FAILS it (negative control), so a green run proves the oracle can tell the
difference.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))
from visual_scenes import SCENES  # noqa: E402

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    np = None

ASSETS = Path(os.environ.get('LISTENING_HUB_ASSETS', ''))
BASELINE = Path(os.environ['LISTENING_O9_BASELINE_ASSETS']) if os.environ.get('LISTENING_O9_BASELINE_ASSETS') else None
REV = os.environ.get('LISTENING_O9_BASELINE_REV', '0750cc4bf7ca1a0f11d2bb72bff97eacab65934a')
W, H, FRAMES = 1440, 810, 480
CHANGED = ('room-midnight-library', 'room-after-hours', 'room-atomic-lounge', 'room-wizard-valley')
CLOUD_ROOMS = ('room-after-hours', 'room-atomic-lounge', 'room-wizard-valley')
LIBRARY = 'room-midnight-library'
# A step may be at most this many times the median step. Smooth drift sits near 1.5;
# the pre-O9 build ticks at 14 to 30.
TICK_FACTOR = 3.0


def have_assets():
    return (np is not None and shutil.which('ffmpeg') and ASSETS.is_dir()
            and (ASSETS / (LIBRARY + '.mp4')).is_file())


def crop_box(rect, pad=0):
    """Even-aligned pixel box (x0, y0, x1, y1) for a fractional rectangle."""
    x0, y0 = int(rect[0]*W) & ~1, int(rect[1]*H) & ~1
    x1, y1 = (int(rect[2]*W) + 2) & ~1, (int(rect[3]*H) + 2) & ~1
    return x0, y0, min(x1, W), min(y1, H)


def decode(assets, identity, box, pix='rgb24'):
    x0, y0, x1, y1 = box
    blob = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(assets / (identity + '.mp4')),
                                    '-vf', 'crop=%d:%d:%d:%d' % (x1 - x0, y1 - y0, x0, y0),
                                    '-f', 'rawvideo', '-pix_fmt', pix, '-'])
    channels = 3 if pix == 'rgb24' else 1
    shape = (-1, y1 - y0, x1 - x0) + ((3,) if channels == 3 else ())
    return np.frombuffer(blob, np.uint8).reshape(shape)


def luma(frames):
    f = frames.astype(np.float32)
    return .3*f[..., 0] + .59*f[..., 1] + .11*f[..., 2]


def steps(grey):
    g = grey.astype(np.float32)
    ring = np.concatenate([g, g[:1]])  # include the wrap step
    return np.abs(np.diff(ring, axis=0)).mean(axis=(1, 2))


def fire_polygon():
    fire = [e for e in SCENES[LIBRARY]['effects'] if e['kind'] == 'fire'][0]
    return fire['polygon']


def firebox_stats(assets):
    """Flame coverage (whole firebox and its top half) and mean luma per frame."""
    poly = [(x*W, y*H) for x, y in fire_polygon()]
    x0 = int(min(p[0] for p in poly)) & ~1
    y0 = int(min(p[1] for p in poly)) & ~1
    x1 = (int(max(p[0] for p in poly)) + 2) & ~1
    y1 = (int(max(p[1] for p in poly)) + 2) & ~1
    mask = Image.new('L', (W, H))
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    mask = np.array(mask)[y0:y1, x0:x1] > 0
    top = np.zeros_like(mask)
    top[:(y1 - y0)//2] = True
    f = decode(assets, LIBRARY, (x0, y0, x1, y1)).astype(np.int32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    flame = (r >= 200) & (r - b >= 100) & (g >= 100)
    return {
        'cover': (flame & mask).sum((1, 2)) / mask.sum(),
        'top': (flame & mask & top).sum((1, 2)) / (mask & top).sum(),
        'luma': (luma(f) * mask).sum((1, 2)) / mask.sum(),
    }


@unittest.skipUnless(have_assets(), 'needs LISTENING_HUB_ASSETS room videos, ffmpeg and numpy')
class LibraryFire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.new = firebox_stats(ASSETS)
        cls.old = firebox_stats(BASELINE) if BASELINE else None

    def test_fire_fills_the_firebox_compared_with_the_old_render(self):
        if self.old is None:
            self.skipTest('needs LISTENING_O9_BASELINE_ASSETS')
        new, old = self.new, self.old
        self.assertGreaterEqual(new['cover'].mean(), 1.5*old['cover'].mean(), 'mean flame coverage')
        self.assertGreaterEqual(new['top'].mean(), 2.5*old['top'].mean(), 'flame in the upper half of the firebox')
        self.assertGreaterEqual(new['cover'].min(), old['cover'].mean(), 'even the weakest frame beats the old average')
        self.assertGreaterEqual(new['luma'].mean(), 1.05*old['luma'].mean(), 'emissive brightness')

    def test_fire_is_not_blown_out_and_still_moves(self):
        new = self.new
        self.assertLess(new['cover'].max(), .30, 'flame must not flood the firebox')
        self.assertLess(new['luma'].mean(), 1.5*(self.old['luma'].mean() if self.old else 66), 'no blown-out glow')
        self.assertGreater(new['cover'].std(), .005, 'coverage must vary over the loop')
        self.assertGreater(new['top'].max() - new['top'].min(), .02, 'tongues must rise and fall')

    def test_absolute_fill_floor_met_by_new_and_missed_by_old(self):
        """Negative control: a fixed floor for the upper firebox that the old render misses."""
        floor = .025
        self.assertGreaterEqual(self.new['top'].mean(), floor, 'new top-half coverage')
        if self.old is None:
            self.skipTest('needs LISTENING_O9_BASELINE_ASSETS')
        self.assertLess(self.old['top'].mean(), floor, 'the old render should miss the floor')

    def test_filled_wall_is_grained_not_a_smudge(self):
        """Where the painted flame was removed the stone must keep fine texture.

        Compare high-frequency luma energy in the wall just above the flame (a region the
        procedural flame rarely reaches at the loop's quietest frames) with the plate wall.
        The per-pixel temporal median over the loop is nearly flame-free there.
        """
        poly = fire_polygon()
        # Wall strip inside the firebox, right of the main tongue: x 62-92%, y 15-60% of the zone.
        px = [p[0] for p in poly]
        py = [p[1] for p in poly]
        zx0, zx1, zy0, zy1 = min(px), max(px), min(py), max(py)
        rect = (zx0 + .62*(zx1 - zx0), zy0 + .15*(zy1 - zy0), zx0 + .92*(zx1 - zx0), zy0 + .60*(zy1 - zy0))
        box = crop_box(rect)
        grey = luma(decode(ASSETS, LIBRARY, box))
        median = np.median(grey, axis=0)
        plate = SCENES[LIBRARY]['plate']
        with Image.open(SOURCE / 'artwork' / plate) as image:
            plate_grey = np.array(image.convert('L').resize((W, H), Image.Resampling.BICUBIC)).astype(np.float32)
        x0, y0, x1, y1 = box
        reference = plate_grey[y0:y1, x0:x1]

        def detail(a):
            return float(np.abs(4*a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]).mean())
        # A smudge loses detail: the filled wall keeps at least a third of the painted wall's.
        self.assertGreaterEqual(detail(median), detail(reference)/3, 'filled wall lost its grain')


@unittest.skipUnless(have_assets(), 'needs LISTENING_HUB_ASSETS room videos, ffmpeg and numpy')
class CloudTick(unittest.TestCase):
    def ratio(self, assets, identity):
        scene = SCENES[identity]
        rect = [e for e in scene['effects'] if e['kind'] == 'cloud'][0]['rect']
        grey = decode(assets, identity, crop_box(rect), 'gray')
        s = steps(grey)
        return float(s.max()/np.median(s)), int(s.argmax())

    def test_clouds_drift_without_a_tick(self):
        for identity in CLOUD_ROOMS:
            with self.subTest(identity):
                ratio, at = self.ratio(ASSETS, identity)
                self.assertLessEqual(ratio, TICK_FACTOR, '%s: largest cloud step is %.1fx the median (frame %d)' % (identity, ratio, at))

    def test_tick_oracle_fails_on_the_old_render(self):
        if BASELINE is None:
            self.skipTest('needs LISTENING_O9_BASELINE_ASSETS')
        for identity in CLOUD_ROOMS:
            with self.subTest(identity):
                ratio, _ = self.ratio(BASELINE, identity)
                self.assertGreater(ratio, TICK_FACTOR, 'the old render should tick')

    def test_no_periodic_step_in_the_whole_frame_either(self):
        # The tick must not just move out of the cloud rectangle: whole-frame steps stay smooth.
        for identity in CLOUD_ROOMS:
            with self.subTest(identity):
                s = steps(decode(ASSETS, identity, (0, 0, W, H), 'gray')[::1])
                self.assertLessEqual(float(s.max()/np.median(s)), 4.0, identity)


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@unittest.skipUnless(have_assets(), 'needs LISTENING_HUB_ASSETS room videos, ffmpeg and numpy')
class O8Guarantees(unittest.TestCase):
    def test_six_atmosphere_files_and_unchanged_rooms_are_byte_identical(self):
        if BASELINE is None:
            self.skipTest('needs LISTENING_O9_BASELINE_ASSETS')
        new, old = files(ASSETS), files(BASELINE)
        self.assertEqual(sorted(new), sorted(old), 'same file set')
        atmospheres = [n for n in new if n.startswith('atmospheres/')]
        self.assertEqual(len([n for n in atmospheres if n.endswith('.wav')]), 6)
        for name in atmospheres:
            self.assertEqual(new[name], old[name], name)
        changed = {c + ext for c in CHANGED for ext in ('.mp4', '.png')}
        for name in new:
            if name.startswith('atmospheres/') or name in changed:
                continue
            self.assertEqual(new[name], old[name], name + ' must be byte-identical to the pre-O9 build')
        for name in changed:
            if name.endswith('.mp4'):
                self.assertNotEqual(new[name], old[name], name + ' should have changed')
        for identity in CHANGED:
            self.assertEqual(new[identity + '-still.jpg'], old[identity + '-still.jpg'], 'reduced-motion still unchanged')

    def test_reduced_motion_still_equals_the_plate(self):
        for identity, scene in SCENES.items():
            with self.subTest(identity):
                plate = SOURCE / 'artwork' / scene['plate']
                still = ASSETS / (identity + '-still.jpg')
                if plate.suffix.lower() in ('.jpg', '.jpeg'):
                    self.assertEqual(still.read_bytes(), plate.read_bytes())
                else:
                    import io
                    out = io.BytesIO()
                    with Image.open(plate) as image:
                        image.convert('RGB').save(out, 'JPEG', quality=92)
                    self.assertEqual(still.read_bytes(), out.getvalue())

    def test_loops_are_seamless_in_the_changed_rooms(self):
        for identity in CHANGED:
            with self.subTest(identity):
                grey = decode(ASSETS, identity, (0, 0, W, H), 'gray').astype(np.float32)
                inner = np.abs(np.diff(grey, axis=0)).mean(axis=(1, 2))
                seam = float(np.abs(grey[0] - grey[-1]).mean())
                self.assertLessEqual(seam, 1.5*inner.max() + .01, 'wrap step vs largest step')
                self.assertLessEqual(seam, 3*np.median(inner) + .05, 'wrap step vs median step')
                self.assertEqual(len(grey), FRAMES)

    @staticmethod
    def rigid_swings(identity, grey):
        scene = SCENES[identity]
        with Image.open(SOURCE / 'artwork' / scene['plate']) as image:
            plate = np.array(image.convert('L').resize((W, H), Image.Resampling.BICUBIC)).astype(np.float32)
        swings = {}
        for region in scene.get('rigid', []):
            mask = Image.new('L', (W, H))
            ImageDraw.Draw(mask).polygon([(x*W, y*H) for x, y in region['polygon']], fill=255)
            inside = (np.array(mask) > 0) & (plate < 45)
            if inside.sum() >= 40:
                series = grey[:, inside].astype(np.int16)
                swings[region['name']] = float(np.percentile(series.max(0) - series.min(0), 90))
        return swings

    def test_rigid_dark_structure_is_still_in_the_changed_rooms(self):
        """Dark structure inside each rigid polygon varies no more than encode noise."""
        for identity in CHANGED:
            swings = self.rigid_swings(identity, decode(ASSETS, identity, (0, 0, W, H), 'gray'))
            self.assertGreaterEqual(len(swings), 3, identity + ': oracle must measure several regions')
            for name, swing in swings.items():
                with self.subTest(identity, region=name):
                    self.assertLessEqual(swing, 4, 'temporal swing (90th percentile) of dark structure')

    def test_rigid_oracle_is_sharp_without_the_rigid_mask(self):
        """Negative control: render the library with its rigid list emptied; the oracle must object."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / 'src'
            shutil.copytree(SOURCE, tree, ignore=shutil.ignore_patterns('models', '__pycache__'))
            path = tree / 'scene_tuning' / 'midnight-library.json'
            data = json.loads(path.read_text())
            data['rigid'] = []
            path.write_text(json.dumps(data))
            script = ("import sys, pathlib; sys.path.insert(0, sys.argv[1]); "
                      "from render_vistas import render_room; from visual_scenes import SCENES; "
                      "i = sys.argv[2]; render_room(pathlib.Path(sys.argv[3]) / (i + '.mp4'), SCENES[i], "
                      "pathlib.Path(sys.argv[1]) / 'artwork' / SCENES[i]['plate'])")
            subprocess.check_call([sys.executable, '-I', '-c', script, str(tree), LIBRARY, tmp])
            mutated = decode(Path(tmp), LIBRARY, (0, 0, W, H), 'gray')
            swings = self.rigid_swings(LIBRARY, mutated)
            self.assertGreater(max(swings.values()), 4, 'without a rigid mask the grate/posts should flicker: ' + str(swings))

    def test_rebuild_is_deterministic(self):
        """Render each changed room twice from the source tree: identical bytes, equal to the build."""
        script = ("import sys, pathlib; sys.path.insert(0, sys.argv[1]); "
                  "from PIL import Image; from render_vistas import render_room; from visual_scenes import SCENES; "
                  "i = sys.argv[2]; render_room(pathlib.Path(sys.argv[3]) / (i + '.mp4'), SCENES[i], "
                  "pathlib.Path(sys.argv[1]) / 'artwork' / SCENES[i]['plate'])")
        for identity in CHANGED:
            with self.subTest(identity), tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
                for out in (one, two):
                    subprocess.check_call([sys.executable, '-I', '-c', script, str(SOURCE), identity, out])
                a = (Path(one) / (identity + '.mp4')).read_bytes()
                b = (Path(two) / (identity + '.mp4')).read_bytes()
                self.assertEqual(hashlib.sha256(a).digest(), hashlib.sha256(b).digest(), 'two renders differ')
                self.assertEqual(hashlib.sha256(a).digest(),
                                 hashlib.sha256((ASSETS / (identity + '.mp4')).read_bytes()).digest(),
                                 'render differs from the built asset')


def baseline_source(target):
    Path(target).mkdir(parents=True, exist_ok=True)
    subprocess.check_call('git -C %s archive %s src | tar -x -C %s' % (ROOT, REV, target), shell=True,
                          stderr=subprocess.DEVNULL)
    return Path(target) / 'src'


class TuningValidation(unittest.TestCase):
    """Black box: copy the source tree, mutate one tuning file, import visual_scenes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)

    def tree(self, name, source=None):
        target = Path(self.tmp) / name
        shutil.copytree(source or SOURCE, target, ignore=shutil.ignore_patterns('artwork', 'models', '__pycache__'))
        return target

    def load(self, tree):
        return subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import visual_scenes',
                               str(tree)], capture_output=True, text=True)

    def mutate(self, name, room, change, source=None):
        tree = self.tree(name, source)
        path = tree / 'scene_tuning' / (room + '.json')
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
        return tree, self.load(tree)

    def fire(self, data):
        return [e for e in data['effects'] if e['kind'] == 'fire'][0]

    def test_unmodified_tree_loads(self):
        result = self.load(self.tree('clean'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def expect_failure(self, name, room, change, *needles):
        _, result = self.mutate(name, room, change)
        self.assertNotEqual(result.returncode, 0, name + ' should fail the load')
        for needle in (room + '.json',) + needles:
            self.assertIn(needle, result.stderr, '%s: message must name %r\n%s' % (name, needle, result.stderr[-600:]))

    def test_unknown_fire_key_names_file_effect_and_parameter(self):
        self.expect_failure('typo', 'midnight-library', lambda d: self.fire(d).update(reech=.3), 'fire', 'reech')

    def test_wrong_type_fails(self):
        self.expect_failure('type', 'midnight-library', lambda d: self.fire(d).update(reach='0.3'), 'fire', 'reach')
        self.expect_failure('type2', 'midnight-library', lambda d: self.fire(d).update(sparks=2.5), 'fire', 'sparks')
        self.expect_failure('type3', 'midnight-library', lambda d: self.fire(d).update(bed=[1]), 'fire', 'bed')

    def test_out_of_range_fails(self):
        for index, (key, value) in enumerate((('reach', 2), ('bed', -.1), ('suppress', 1.5), ('heat', 99),
                                                ('tongue', 0), ('flame_threshold', 300), ('grain', 3))):
            self.expect_failure('range%d' % index, 'midnight-library', lambda d: self.fire(d).update({key: value}), 'fire', key)

    def test_out_of_range_other_effects_fail(self):
        def room_effect(room, kind):
            return lambda change: (lambda d: change([e for e in d['effects'] if e['kind'] == kind][0]))
        self.expect_failure('cloud', 'after-hours', room_effect('after-hours', 'cloud')(lambda e: e.update(amplitude=-5)), 'cloud', 'amplitude')
        self.expect_failure('cloudkey', 'wizard-valley', room_effect('wizard-valley', 'cloud')(lambda e: e.update(amplitde=5)), 'cloud', 'amplitde')
        self.expect_failure('strength', 'atomic-lounge', room_effect('atomic-lounge', 'cloud')(lambda e: e.update(strength=7)), 'cloud', 'strength')

    def test_unknown_effect_kind_fails(self):
        def change(data):
            self.fire(data)['kind'] = 'flame'
        self.expect_failure('kind', 'midnight-library', change, 'flame')

    def test_lick_is_gone(self):
        # R-src1 (acceptance author, WP4): the source-text half moved to test_lick_is_gone_from_the_source_text (skipped,
        # ORIGINAL) and EngineReads.test_the_engine_reads_no_lick_parameter; the shipped data and the loader stay here.
        for path in (SOURCE / 'scene_tuning').glob('*.json'):
            self.assertNotIn('"lick"', path.read_text(), path.name)
        self.expect_failure('lick', 'midnight-library', lambda d: self.fire(d).update(lick=.9), 'fire', 'lick')

    @unittest.skip('ORIGINAL, superseded by EngineReads.test_the_engine_reads_no_lick_parameter (R-src1, acceptance author, WP4): '
                   "a text search for 'lick' in every module, which comments or unrelated names could satisfy or break")
    def test_lick_is_gone_from_the_source_text(self):
        for path in SOURCE.glob('*.py'):
            self.assertNotIn("'lick'", path.read_text(), path.name)

    @unittest.skip('ORIGINAL, superseded by EngineReads.test_every_tuning_key_is_read_by_the_engine_at_run_time (R-src1, acceptance '
                   'author, WP4): it passed when the quoted key appeared anywhere in motion_engine.py, so a key the effect never reads '
                   "(the electrical and water 'color' keys) still passed")
    def test_every_tuning_key_is_read_by_the_engine(self):
        engine = (SOURCE / 'motion_engine.py').read_text()
        for path in sorted((SOURCE / 'scene_tuning').glob('*.json')):
            data = json.loads(path.read_text())
            for effect in data.get('effects', []):
                for key in effect:
                    self.assertIn("'%s'" % key, engine, '%s: %s key %r is never read' % (path.name, effect['kind'], key))

    def test_rigid_and_top_level_typos_fail(self):
        self.expect_failure('rigid', 'after-hours', lambda d: d['rigid'][0].update(polgon=[]), 'rigid', 'polgon')
        self.expect_failure('top', 'after-hours', lambda d: d.update(effectz=[]), 'effectz')

    def test_oracle_is_sharp_old_source_accepts_the_typo(self):
        """Negative control: the pre-O9 source loads a typo silently."""
        try:
            old = baseline_source(Path(self.tmp) / 'oldsrc')
        except (subprocess.CalledProcessError, OSError):
            self.skipTest('baseline revision not available')
        tree, result = self.mutate('oldtypo', 'midnight-library', lambda d: self.fire(d).update(reech=.3), source=old)
        self.assertEqual(result.returncode, 0, 'baseline unexpectedly rejects typos: ' + result.stderr[-300:])


class EngineReads(unittest.TestCase):
    """R-src1 (acceptance author, WP4): which tuning keys the engine actually reads while it renders each room.

    The engine copies each effect with dict(); a recording dict in its place sees every key it looks up while preparing the
    scene and rendering frames across the loop. Replaces the source-text searches of motion_engine.py.
    """
    # [ORIGINAL, superseded by the empty KNOWN_UNREAD below (O10-HYGIENE acceptance author, coordinator decision: the 17
    # unread `color` keys are removed, not honoured). Kept as it was at e45d947 so the history stays visible; it is data,
    # no longer the list this oracle tolerates (see UNREAD_AT_E45D947).]
    # Shipped keys that the engine never reads for their own effect: the electrical and water branches finish before the
    # shared colour step. Recorded at main 25cda4a as a finding, held exactly so a new dead key or a fix both show up.
    UNREAD_AT_E45D947 = {('after-hours', 2, 'water', 'color')} | {
        (room, index, 'electrical', 'color') for room, indices in (('after-hours', (5, 6, 7, 8, 9)), ('atomic-lounge', (0, 1, 2, 3, 4, 5)),
                                                                   ('midnight-library', (5, 6)), ('velvet-circuit', (0, 1, 2)))
        for index in indices}
    # Revision (O10-HYGIENE): no shipped key may be unread. The test below still reports any unread key as a failure, so a
    # newly dead key is caught exactly as before; a key that is read again would be a rendering change, which the
    # O10-HYGIENE oracles (test_listening_o10h_oracles.py) check separately against the e45d947 tuning.
    KNOWN_UNREAD = set()

    @classmethod
    def render(cls, scene):
        import motion_engine
        records = []

        class Recording(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.read = set()
                records.append(self)

            def __getitem__(self, key):
                self.read.add(key)
                return super().__getitem__(key)

            def get(self, key, *default):
                self.read.add(key)
                return super().get(key, *default)

            def __contains__(self, key):
                self.read.add(key)
                return super().__contains__(key)

            def setdefault(self, key, *default):
                self.read.add(key)
                return super().setdefault(key, *default)

            def pop(self, key, *default):
                self.read.add(key)
                return super().pop(key, *default)

            def _everything(self):
                self.read |= set(super().keys())

            def keys(self):
                self._everything()
                return super().keys()

            def items(self):
                self._everything()
                return super().items()

            def values(self):
                self._everything()
                return super().values()

            def __iter__(self):
                self._everything()
                return super().__iter__()
        motion_engine.dict = Recording
        try:
            with Image.open(SOURCE / 'artwork' / scene['plate']) as plate:
                prepared = motion_engine.prepare_scene(plate, scene)
            for frame in (0, 97, 240, 401):
                motion_engine.room_frame(prepared, frame, 480)
        finally:
            del motion_engine.dict
        effects = [r for r in records if 'kind' in r]
        assert [e['kind'] for e in effects] == [e['kind'] for e in scene['effects']], 'every effect is copied once, in order'
        return effects

    @classmethod
    def setUpClass(cls):
        if np is None:
            raise unittest.SkipTest('needs Pillow and numpy')
        cls.unread, cls.read_anywhere, cls.keys = set(), set(), set()
        for path in sorted((SOURCE / 'scene_tuning').glob('*.json')):
            data = json.loads(path.read_text())
            effects = cls.render(SCENES['room-' + path.stem])
            for index, (effect, seen) in enumerate(zip(data.get('effects', []), effects)):
                cls.keys |= set(effect)
                cls.read_anywhere |= seen.read
                cls.unread |= {(path.stem, index, effect['kind'], key) for key in set(effect) - seen.read}

    def test_every_tuning_key_is_read_by_the_engine_at_run_time(self):
        # The original guarantee at its original strength: no shipped key is one the engine never looks up.
        self.assertEqual(sorted(self.keys - self.read_anywhere), [])

    def test_unread_keys_on_their_own_effect_are_exactly_the_known_ones(self):
        self.assertEqual(sorted(self.unread - self.KNOWN_UNREAD), [], 'shipped keys the engine never reads for their effect')
        self.assertEqual(sorted(self.KNOWN_UNREAD - self.unread), [], 'a known unread key is now read or gone: shrink KNOWN_UNREAD')

    def test_the_seventeen_keys_held_at_e45d947_are_gone(self):
        # O10-HYGIENE revision: the keys listed in UNREAD_AT_E45D947 must no longer be shipped at all (removed, not honoured).
        shipped = {(path.stem, index, effect['kind'], key)
                   for path in sorted((SOURCE / 'scene_tuning').glob('*.json'))
                   for index, effect in enumerate(json.loads(path.read_text()).get('effects', [])) for key in effect}
        self.assertEqual(len(self.UNREAD_AT_E45D947), 17)
        self.assertEqual(sorted(self.UNREAD_AT_E45D947 & shipped), [], 'unread colour keys still shipped')

    def test_the_engine_reads_no_lick_parameter(self):
        scene = json.loads(json.dumps(SCENES['room-midnight-library']))
        fire = [e for e in scene['effects'] if e['kind'] == 'fire'][0]
        fire['lick'] = .9
        effects = self.render(scene)
        fired = [e for e in effects if e['kind'] == 'fire'][0]
        self.assertIn('reach', fired.read)          # control: the recording does see the fire's real parameters
        self.assertNotIn('lick', fired.read)


if __name__ == '__main__':
    unittest.main()
