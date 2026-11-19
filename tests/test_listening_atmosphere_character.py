"""Objective oracles: each offline sound has the character its label promises.

These measure the shipped beds (24 kHz, forty seconds) with small standard-library
analysers. The thresholds were calibrated on the rendered beds and on a white noise
control; each comment names the property being asserted, not just a number.
Run `python3 tests/test_listening_atmosphere_character.py --features` to print the
measured feature table.
"""
from array import array
import cmath
import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import atmosphere_catalogue as catalogue
import render_atmospheres as render

RATE = render.RATE
IDS = [item['id'] for item in catalogue.SOUNDS]
_cache = {}


def bed(identity):
    if identity not in _cache:
        _cache[identity] = [x / 32768 for x in render.render(identity)]
    return _cache[identity]


def white_noise(count=RATE * render.SECONDS):
    rng = random.Random(7)
    return [rng.gauss(0, .08) for _ in range(count)]


def fft(values):
    n = len(values)
    if n == 1:
        return list(values)
    even, odd = fft(values[0::2]), fft(values[1::2])
    twiddled = [cmath.exp(-2j * math.pi * k / n) * odd[k] for k in range(n // 2)]
    return [even[k] + twiddled[k] for k in range(n // 2)] + [even[k] - twiddled[k] for k in range(n // 2)]


def band_density(x, frames=24, size=4096):
    """Welch power per Hz in third-octave bands from 63 Hz to 10 kHz: [(centre, density)]."""
    window = [.5 - .5 * math.cos(math.tau * i / size) for i in range(size)]
    power = [0.0] * (size // 2)
    for f in range(frames):
        start = (len(x) - size) * f // (frames - 1)
        spectrum = fft([a * b for a, b in zip(x[start:start + size], window)])
        for k in range(size // 2):
            power[k] += abs(spectrum[k]) ** 2
    bins = RATE / size
    bands = []
    centre = 63.0
    while centre <= 10000:
        lo, hi = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        selected = power[max(1, int(lo / bins)):max(2, int(hi / bins))]
        bands.append((centre, sum(selected) / len(selected)))
        centre *= 2 ** (1 / 3)
    return bands


def flatness_and_tilt(x):
    """Spectral flatness (1 = white) and tilt in dB per octave over third-octave bands."""
    bands = band_density(x)
    logs = [math.log(d + 1e-30) for _, d in bands]
    flatness = math.exp(sum(logs) / len(logs)) / (sum(d for _, d in bands) / len(bands))
    octaves = [math.log2(c) for c, _ in bands]
    db = [10 * math.log10(d + 1e-30) for _, d in bands]
    mo, md = sum(octaves) / len(octaves), sum(db) / len(db)
    tilt = sum((o - mo) * (v - md) for o, v in zip(octaves, db)) / sum((o - mo) ** 2 for o in octaves)
    return flatness, tilt


def block_energy(values, size):
    return [sum(v * v for v in values[i:i + size]) for i in range(0, len(values) - size + 1, size)]


def diff(x):
    return [x[i] - x[i - 1] for i in range(1, len(x))]


def onset_rate(x, hop=64, rise_db=6.0):
    """Sharp rises per second in the high-emphasised (first-difference) energy."""
    energy = block_energy(diff(x), hop)
    db = [10 * math.log10(e + 1e-12) for e in energy]
    count, last = 0, -10
    for i in range(4, len(db) - 1):
        if db[i] - min(db[i - 4:i]) >= rise_db and db[i] >= db[i + 1] - 1 and i - last > 3:
            count += 1
            last = i
    return count / (len(x) / RATE)


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def slow_modulation_depth(x, window=.1):
    """(p90 - p10) / (p90 + p10) of the broadband RMS envelope: slow gust-like swell."""
    size = int(window * RATE)
    rms = [math.sqrt(e / size) for e in block_energy(x, size)]
    lo, hi = percentile(rms, .1), percentile(rms, .9)
    return (hi - lo) / (hi + lo)


def band_depth_and_correlation(x):
    """Depth of the rustle band (first difference) envelope and its correlation with the body."""
    size = int(.1 * RATE)
    high = [math.sqrt(e / size) for e in block_energy(diff(x), size)]
    total = [math.sqrt(e / size) for e in block_energy(x, size)]
    lo, hi = percentile(high, .1), percentile(high, .9)
    n = min(len(high), len(total))
    mh, mt = sum(high[:n]) / n, sum(total[:n]) / n
    cov = sum((a - mh) * (b - mt) for a, b in zip(high, total))
    norm = math.sqrt(sum((a - mh) ** 2 for a in high[:n]) * sum((b - mt) ** 2 for b in total[:n]))
    return (hi - lo) / (hi + lo), cov / norm


def low_frequency_events(x, block=200):
    """Rumble events under ~100 Hz: [(start seconds, length seconds)], and the p95/p10 range."""
    means = [abs(sum(x[i:i + block])) / block for i in range(0, len(x) - block + 1, block)]
    rate = RATE / block
    smooth, window = [], int(.5 * rate)
    run = sum(means[:window])
    for i in range(len(means)):
        smooth.append(run / window)
        run += means[(i + window) % len(means)] - means[i]
    floor = percentile(smooth, .5)
    threshold = 2.5 * floor
    events, i = [], 0
    start = next((i for i, v in enumerate(smooth) if v < threshold), 0)
    order = smooth[start:] + smooth[:start]
    index = 0
    while index < len(order):
        if order[index] > threshold:
            begin = index
            while index < len(order) and order[index] > threshold:
                index += 1
            if (index - begin) / rate >= 1.5:
                events.append((((begin + start) % len(smooth)) / rate, (index - begin) / rate))
        index += 1
    return events, percentile(smooth, .95) / max(percentile(smooth, .1), 1e-12)


def clack_periodicity(x, hop=240):
    """Strongest normalised autocorrelation of the detrended clack envelope, its lag, and the
    correlation at the 0.15 s pair gap."""
    energy = [math.sqrt(e) for e in block_energy(diff(x), hop)]
    rate = RATE / hop
    n = len(energy)
    window = int(rate)
    run = sum(energy[-window:])
    env = []
    for i in range(n):  # subtract a one-second moving average so slow swells do not count
        env.append(energy[i] - run / window)
        run += energy[(i + 1) % n] - energy[(i + 1 - window) % n]
    norm = sum(e * e for e in env)

    def at(lag):
        return sum(env[i] * env[(i + lag) % n] for i in range(n)) / norm
    best = (0.0, 0.0)
    for lag in range(int(.5 * rate), int(1.6 * rate)):
        value = at(lag)
        if value > best[0]:
            best = (value, lag / rate)
    pair_lag = int(.15 * rate)
    return best[0], best[1], max(at(pair_lag - 1), at(pair_lag), at(pair_lag + 1))


def mid_band(x, low, high, q_scale=1.0):
    """Band-limit 300-3400 Hz speech region by decimating to 6 kHz and two biquads."""
    decimated = array('f', (sum(x[i:i + 4]) / 4 for i in range(0, len(x) - 3, 4)))
    rate = RATE / 4
    shaped = render.biquad(decimated, 'hp', low, .7, rate)
    return render.biquad(shaped, 'lp', high, .7, rate), rate


def syllabic_modulation(x):
    """Std/mean of the 300-3400 Hz envelope restricted to 2-8 Hz: speech-rate formant modulation."""
    band, rate = mid_band(x, 300, 2400)
    size = int(rate * .02)
    env = [math.sqrt(e / size) for e in block_energy(band, size)]
    env_rate = rate / size
    mean = sum(env) / len(env)
    n = len(env)
    power = 0.0
    for freq in [2 + .25 * k for k in range(25)]:
        c = sum((e - mean) * cmath.exp(-2j * math.pi * freq * i / env_rate) for i, e in enumerate(env)) / n
        power += abs(c) ** 2 * 2
    return math.sqrt(power) / mean


def voiced_fraction(x):
    """Share of 20 ms frames in the 300-2400 Hz band with a clear 95-240 Hz pitch periodicity."""
    band, rate = mid_band(x, 300, 2400)
    size = int(rate * .02)
    voiced = frames = 0
    for start in range(0, len(band) - size - 64, size * 2):
        frame = band[start:start + size]
        energy = sum(v * v for v in frame)
        if energy < 1e-9:
            continue
        frames += 1
        best = max(sum(frame[i] * band[start + i + lag] for i in range(size)) for lag in range(25, 64))
        voiced += best / energy > .5
    return voiced / max(frames, 1)


def features(identity):
    x = bed(identity)
    flat, tilt = flatness_and_tilt(x)
    events, lf_range = low_frequency_events(x)
    band_depth, band_corr = band_depth_and_correlation(x)
    peak, lag, pair = clack_periodicity(x)
    return {'flatness': flat, 'tilt': tilt, 'onsets': onset_rate(x),
            'lf_events': len(events), 'lf_range': lf_range,
            'slow_depth': slow_modulation_depth(x), 'rustle_depth': band_depth,
            'rustle_corr': band_corr, 'periodicity': peak, 'period_lag': lag,
            'pair': pair, 'syllabic': syllabic_modulation(x),
            'voiced': voiced_fraction(x)}


class CharacterOracles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = {identity: features(identity) for identity in IDS}
        control = white_noise()
        cls.control = dict(zip(('flatness', 'tilt'), flatness_and_tilt(control)))
        cls.control.update(onsets=onset_rate(control), slow_depth=slow_modulation_depth(control),
                           periodicity=clack_periodicity(control)[0],
                           syllabic=syllabic_modulation(control),
                           lf_events=len(low_frequency_events(control)[0]))

    def test_the_oracles_see_white_noise_as_white(self):
        # Guards the instruments: a real white noise control must fail every "character" test.
        self.assertGreater(self.control['flatness'], .85)
        self.assertLess(abs(self.control['tilt']), 1.0)
        self.assertLess(self.control['onsets'], 1)
        self.assertLess(self.control['slow_depth'], .1)
        self.assertLess(self.control['periodicity'], .2)
        self.assertEqual(self.control['lf_events'], 0)

    def test_no_bed_is_flat_and_each_has_a_tilt_that_suits_it(self):
        # (max flatness, max tilt in dB per octave); white noise is about 1.0 and 0.
        # Rain is the bright one (a wash above 1 kHz), the rest fall toward the treble.
        limits = {'fireplace': (.45, -1.5), 'distant-thunder': (.1, -6.0), 'forest-wind': (.65, -1.0),
                  'train-carriage': (.15, -4.0), 'cafe-murmur': (.55, -1.0)}
        rain = self.table['rain-glass']
        self.assertLess(rain['flatness'], .45)
        self.assertGreater(rain['tilt'], 0)
        for identity, (flat, tilt) in limits.items():
            with self.subTest(sound=identity):
                self.assertLess(self.table[identity]['flatness'], flat)
                self.assertLess(self.table[identity]['tilt'], tilt)

    def test_rain_and_fire_are_made_of_onsets(self):
        self.assertGreater(self.table['rain-glass']['onsets'], 15)
        self.assertGreater(self.table['fireplace']['onsets'], 4)
        for identity in ('distant-thunder', 'forest-wind', 'train-carriage'):
            self.assertLess(self.table[identity]['onsets'], self.table['rain-glass']['onsets'])

    def test_thunder_is_a_few_irregular_low_rolls(self):
        t = self.table['distant-thunder']
        self.assertGreaterEqual(t['lf_events'], 3)
        self.assertLessEqual(t['lf_events'], 10)
        self.assertGreater(t['lf_range'], 6)
        starts = sorted(start for start, _ in low_frequency_events(bed('distant-thunder'))[0])
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        mean = sum(gaps) / len(gaps)
        self.assertGreater(math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps)) / mean, .1)
        for identity in IDS:
            if identity != 'distant-thunder':
                self.assertLess(self.table[identity]['lf_range'], t['lf_range'] / 2)

    def test_rain_under_thunder_stays_audible_between_the_rolls(self):
        x = bed('distant-thunder')
        second = RATE
        levels = [math.sqrt(sum(v * v for v in x[i:i + second]) / second) for i in range(0, len(x) - second + 1, second // 2)]
        self.assertGreater(20 * math.log10(min(levels)), -40.0)

    def test_wind_swells_slowly_and_the_rustle_follows_the_gusts(self):
        w = self.table['forest-wind']
        self.assertGreater(w['slow_depth'], .5)
        self.assertGreater(w['rustle_depth'], .6)
        self.assertGreater(w['rustle_corr'], .7)
        for identity in ('train-carriage', 'cafe-murmur', 'rain-glass'):
            self.assertLess(self.table[identity]['slow_depth'], w['slow_depth'] * .75)

    def test_train_clacks_repeat_in_pairs_at_a_plausible_interval(self):
        t = self.table['train-carriage']
        self.assertGreater(t['periodicity'], .6)
        self.assertGreater(t['period_lag'], .6)
        self.assertLess(t['period_lag'], 1.3)
        self.assertGreater(t['pair'], .2)
        for identity in IDS:
            if identity != 'train-carriage':
                self.assertLess(self.table[identity]['periodicity'], .5)

    def test_cafe_has_voiced_formant_texture_modulated_at_speech_rates(self):
        c = self.table['cafe-murmur']
        # Pitched, formant-shaped frames, and a 2-8 Hz envelope that white noise lacks.
        self.assertGreater(c['voiced'], .2)
        self.assertGreater(c['syllabic'], 4 * self.control['syllabic'])
        for identity in IDS:
            if identity != 'cafe-murmur':
                self.assertLess(self.table[identity]['voiced'], c['voiced'] * .7)

    def test_the_six_sounds_are_mutually_distinguishable(self):
        names = ('tilt', 'onsets', 'lf_range', 'slow_depth', 'periodicity', 'voiced', 'flatness')
        span = {n: max(self.table[i][n] for i in IDS) - min(self.table[i][n] for i in IDS) for n in names}
        for a in IDS:
            for b in IDS:
                if a < b:
                    apart = [n for n in names
                             if abs(self.table[a][n] - self.table[b][n]) >= .25 * span[n]]
                    with self.subTest(pair=(a, b)):
                        self.assertGreaterEqual(len(apart), 2, apart)

    def test_loops_are_seamless_unclipped_and_equally_loud(self):
        # Equally loud means equal A-weighted level (what ordinary speakers make audible), within 1.5 dB of the stated
        # target; the raw RMS stays below -12 dBFS so matching never costs headroom. (Revision: this used to pin raw RMS.)
        target = render.TARGET_RMS
        for identity in IDS:
            x = bed(identity)
            with self.subTest(sound=identity):
                rms = math.sqrt(sum(v * v for v in x) / len(x))
                self.assertLess(20 * math.log10(rms), -12.0)
                self.assertLess(abs(20 * math.log10(render.a_weighted_rms(x) / target)), 1.5)
                self.assertLess(max(abs(v) for v in x), render.PEAK_CEILING + .001)
                self.assertEqual(sum(1 for v in x if abs(v) >= .99), 0)
                steps = [abs(x[i] - x[i - 1]) for i in range(1, len(x))]
                seam = max(abs(x[0] - x[-1]), max(steps[:8]), max(steps[-8:]))
                self.assertLessEqual(seam, percentile(steps, .999) * 1.5)

    def test_rendering_is_byte_deterministic(self):
        for identity in IDS:
            with self.subTest(sound=identity):
                self.assertEqual(render.render(identity, rate=RATE, seconds=6),
                                 render.render(identity, rate=RATE, seconds=6))


if __name__ == '__main__':
    if '--features' in sys.argv:
        sys.argv.remove('--features')
        for identity in IDS:
            print(identity, {k: round(v, 3) for k, v in features(identity).items()}, flush=True)
        c = white_noise()
        print('white', voiced_fraction(c), flatness_and_tilt(c), onset_rate(c), syllabic_modulation(c), clack_periodicity(c)[0],
              slow_modulation_depth(c))
    else:
        unittest.main()
