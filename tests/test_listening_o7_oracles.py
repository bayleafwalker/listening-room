"""Independent acceptance oracles for O7 (offline ambience that sounds like its label).

Owned by the acceptance author, not the implementer. These checks use their own feature
definitions (FFT band-pass, detrended envelopes, adaptive-baseline onset detection, frame
autocorrelation) and calibrate every threshold against noise controls (white, pink, brown) and
against deliberately broken copies of the beds, so a filtered-noise bed cannot pass them.

The beds are rendered once through the same entry point the asset build uses (`write_assets`),
then read back from the WAV files.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
import atmosphere_catalogue as catalogue
import render_atmospheres as render

R = render.RATE
IDS = [item['id'] for item in catalogue.SOUNDS]
_cache = {}


def beds():
    """Rendered beds. With O7_ORACLE_BEDS_DIR set (negative-control runs) the WAVs are read from that directory
    instead; manifest and determinism tests are then meaningless and are expected to fail."""
    alternative = os.environ.get('O7_ORACLE_BEDS_DIR')
    if alternative and not _cache:
        for identity in IDS:
            with wave.open(str(Path(alternative) / (identity + '.wav'))) as w:
                _cache[identity] = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float) / 32768
        _cache['manifest'] = {}
    if not _cache:
        tmp = tempfile.TemporaryDirectory()
        _cache['tmp'] = tmp
        _cache['manifest'] = render.write_assets(tmp.name)
        for identity in IDS:
            with wave.open(str(Path(tmp.name) / 'atmospheres' / (identity + '.wav'))) as w:
                assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, R)
                _cache[identity] = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float) / 32768
    return _cache


def controls():
    rng = np.random.default_rng(7)
    white = rng.standard_normal(R * 40)
    f = np.fft.rfftfreq(len(white), 1 / R)
    out = {'white': white}
    for name, power in (('pink', .5), ('brown', 1.0)):
        X = np.fft.rfft(white)
        X[1:] /= f[1:] ** power
        out[name] = np.fft.irfft(X, len(white))
    return {k: v * .08 / v.std() for k, v in out.items()}


# --- analysis primitives (independent of the implementer's) -------------------------------------------

def bandpass(x, lo, hi):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / R)
    X[(f < lo) | (f >= hi)] = 0
    return np.fft.irfft(X, len(x))


def smooth(a, n):
    n = max(1, int(n))
    kernel = np.zeros(len(a))
    kernel[:n] = 1.0 / n
    kernel = np.roll(kernel, -(n // 2))
    return np.fft.irfft(np.fft.rfft(a) * np.fft.rfft(kernel), len(a))


def envelope(x, seconds):
    return np.sqrt(np.maximum(smooth(x * x, seconds * R), 0))


def welch(x, seg=8192):
    window = np.hanning(seg)
    total = 0
    count = 0
    for s in range(0, len(x) - seg, seg // 2):
        total = total + np.abs(np.fft.rfft(x[s:s + seg] * window)) ** 2
        count += 1
    return np.fft.rfftfreq(seg, 1 / R), total / count


def third_octaves(x):
    f, P = welch(x)
    centres, levels = [], []
    fc = 63.0
    while fc < 9000:
        m = (f >= fc / 2 ** (1 / 6)) & (f < fc * 2 ** (1 / 6))
        centres.append(fc)
        levels.append(10 * np.log10(P[m].mean() + 1e-20))
        fc *= 2 ** (1 / 3)
    return np.array(centres), np.array(levels)


def spectral_shape(x):
    """(tilt dB/octave, rms residual of the third-octave spectrum about its straight-line fit)."""
    c, d = third_octaves(x)
    octave = np.log2(c)
    fit = np.polyfit(octave, d, 1)
    return fit[0], float((d - np.polyval(fit, octave)).std())


def spectral_flatness(x):
    f, P = welch(x)
    p = P[(f > 50) & (f < 10000)]
    return float(np.exp(np.mean(np.log(p))) / np.mean(p))


def power_fraction(x, lo, hi):
    f, P = welch(x)
    return float(P[(f >= lo) & (f < hi)].sum() / P[f >= 20].sum())


def onsets(x, lo=1500, k=2.0, refractory=.004):
    """Peaks of the high-passed 1 ms envelope that exceed k times its own 0.5 s local mean."""
    e = envelope(bandpass(x, lo, R / 2 * .99), .001)
    ratio = e / np.maximum(smooth(e, .5 * R), 1e-9)
    cand = np.flatnonzero((e[1:-1] > e[:-2]) & (e[1:-1] >= e[2:]) & (ratio[1:-1] > k)) + 1
    peaks = []
    for c in cand:
        if not peaks or c - peaks[-1] > refractory * R:
            peaks.append(c)
        elif e[c] > e[peaks[-1]]:
            peaks[-1] = c
    return len(peaks) / (len(x) / R), (ratio[np.array(peaks, int)] if peaks else np.array([]))


def low_events(x):
    """Rolls: peaks of the 20-120 Hz envelope (0.3 s smoothing, 50 Hz grid), >2x median, 2 s apart."""
    e = envelope(bandpass(x, 20, 120), .3)[::480]
    sr = 50
    thr = 2.0 * np.median(e)
    peaks = []
    for i in range(1, len(e) - 1):
        if e[i] >= e[i - 1] and e[i] > e[i + 1] and e[i] > thr:
            if peaks and i - peaks[-1] < 2 * sr:
                if e[i] > e[peaks[-1]]:
                    peaks[-1] = i
            else:
                peaks.append(i)
    dynamic = 20 * np.log10(np.percentile(e, 95) / max(np.percentile(e, 5), 1e-9))
    return len(peaks), float(dynamic)


def gust_modulation(x):
    """(depth dB of the 300-8000 Hz envelope, share of its variance below 0.7 Hz, corr(low, rustle band))."""
    e = envelope(bandpass(x, 300, 8000), .25)[::240]
    depth = 20 * np.log10(np.percentile(e, 90) / max(np.percentile(e, 10), 1e-9))
    a = e - e.mean()
    P = np.abs(np.fft.rfft(a)) ** 2
    f = np.fft.rfftfreq(len(a), 1 / 100)
    slow = P[(f > .05) & (f < .7)].sum() / P[(f > 0) & (f < 8)].sum()
    lo = envelope(bandpass(x, 30, 500), .5)[::480]
    hi = envelope(bandpass(x, 2000, 8000), .5)[::480]
    return float(depth), float(slow), float(np.corrcoef(lo, hi)[0, 1])


def rustle_depth(x):
    hi = envelope(bandpass(x, 2000, 8000), .5)[::480]
    return 20 * np.log10(np.percentile(hi, 90) / max(np.percentile(hi, 10), 1e-9))


def clack_periodicity(x):
    """Autocorrelation of the detrended 60-1500 Hz envelope: (peak, lag s) in 0.5-1.6 s and (peak, lag s) in 0.1-0.22 s."""
    e = envelope(bandpass(x, 60, 1500), .004)[::48]
    sr = 500
    e = e - smooth(e, .4 * sr)
    ac = np.fft.irfft(np.abs(np.fft.rfft(e)) ** 2, len(e))
    ac /= ac[0]
    a, b = int(.5 * sr), int(1.6 * sr)
    k = a + int(np.argmax(ac[a:b]))
    pa, pb = int(.1 * sr), int(.22 * sr)
    kp = pa + int(np.argmax(ac[pa:pb]))
    return float(ac[k]), k / sr, float(ac[kp]), kp / sr


def voiced_fraction(x, threshold=.5):
    """Share of 30 ms frames of the 300-3000 Hz band whose autocorrelation peaks (90-260 Hz lag) above threshold."""
    b = bandpass(x, 300, 3000)
    n = int(.03 * R)
    frames = np.lib.stride_tricks.sliding_window_view(b, n)[::n // 2]
    frames = frames - frames.mean(1, keepdims=True)
    P = np.abs(np.fft.rfft(frames, 2 * n, axis=1)) ** 2
    ac = np.fft.irfft(P, axis=1)[:, :n]
    ac = ac / (ac[:, :1] + 1e-18)
    return float((ac[:, int(R / 260):int(R / 90)].max(1) > threshold).mean())


def syllabic_share(x):
    """Share of the 0.1-20 Hz modulation power of the 300-3000 Hz 10 ms envelope that lies in 2-8 Hz."""
    e = envelope(bandpass(x, 300, 3000), .01)[::240]
    a = e - e.mean()
    P = np.abs(np.fft.rfft(a)) ** 2
    f = np.fft.rfftfreq(len(a), 1 / 100)
    return float(P[(f >= 2) & (f <= 8)].sum() / P[(f > .1) & (f < 20)].sum())


def a_weight_db(f):
    f2 = f.astype(float) ** 2
    ra = (12194 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194 ** 2))
    return 20 * np.log10(np.maximum(ra, 1e-12)) + 2.0


def a_weighted_level(x):
    f, P = welch(x)
    return float(10 * np.log10((P * 10 ** (a_weight_db(np.maximum(f, 1)) / 10))[f >= 20].sum()))


def seam_excess(x):
    """Click score at the loop join: the largest difference within +-2 ms of the seam over the 99.99th percentile
    of the rest of the tiled bed, for the raw signal (2nd difference) and its sub-300 Hz part (1st difference; a step
    hides in a dense bright bed but not there)."""
    n = len(x)
    w = int(.002 * R)
    tiled = np.concatenate([x, x])
    score = 0.0
    for order, signal in ((2, tiled), (1, bandpass(tiled, 0, 300))):
        d = np.abs(np.diff(signal, order))
        edge = int(.1 * R)
        rest = np.concatenate([d[edge:n - w], d[n + w:-edge]])
        score = max(score, float(d[n - w:n + w].max() / np.percentile(rest, 99.99)))
    # local-mean step over 1 ms either side, compared with the same statistic at every other position
    m = int(.001 * R)
    c = np.concatenate([[0], np.cumsum(tiled)])
    idx = np.arange(m, 2 * n - m)
    step = np.abs((c[idx + m] - c[idx]) - (c[idx] - c[idx - m])) / m
    at = n - m
    others = np.concatenate([step[:at - m], step[at + m:]])
    return max(score, float(step[at - 2:at + 3].max() / np.percentile(others, 99.9)))


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)))


# --- oracle tests -----------------------------------------------------------------------------------------

class SpectrumIsNotWhiteNorSimplePink(unittest.TestCase):
    def test_controls_are_what_they_claim(self):
        c = controls()
        self.assertLess(spectral_shape(c['white'])[1], .5)
        self.assertGreater(spectral_shape(c['white'])[0], -.3)
        self.assertAlmostEqual(spectral_shape(c['pink'])[0], -3.0, delta=.3)
        # the control is not distinguishable from the bed by "tilt" alone, which is why shape residual is tested
        self.assertLess(spectral_shape(c['pink'])[1], .5)

    def test_every_bed_is_clearly_not_a_straight_line_spectrum(self):
        for identity in IDS:
            with self.subTest(sound=identity):
                tilt, residual = spectral_shape(beds()[identity])
                # white, pink and brown noise all sit below 0.5 dB. Thunder is the smoothest bed (a very steep
                # rumble; its proof of character is temporal, below), every other bed is far above.
                self.assertGreater(residual, 1.0 if identity == 'distant-thunder' else 2.0, (tilt, residual))
                self.assertLess(spectral_flatness(beds()[identity]), .8)
        self.assertGreater(spectral_flatness(controls()['white']), .95)
        for name, x in controls().items():
            self.assertLess(spectral_shape(x)[1], .5, name)

    def test_tilt_suits_each_sound(self):
        t = {i: spectral_shape(beds()[i])[0] for i in IDS}
        self.assertGreater(t['rain-glass'], 0, t)            # bright: energy rises toward the droplet region
        self.assertLess(t['distant-thunder'], -5, t)         # deep
        self.assertLess(t['train-carriage'], -4, t)          # low rumble
        for identity in ('fireplace', 'forest-wind', 'cafe-murmur'):
            self.assertLess(t[identity], 0, t)
        self.assertLess(t['distant-thunder'], t['train-carriage'])

    def test_where_the_energy_lives(self):
        b = beds()
        self.assertGreater(power_fraction(b['distant-thunder'], 20, 150), .85)
        self.assertGreater(power_fraction(b['train-carriage'], 20, 250), .6)
        self.assertGreater(power_fraction(b['fireplace'], 20, 250), .1)
        self.assertGreater(power_fraction(b['fireplace'], 20, 250), 5 * power_fraction(b['rain-glass'], 20, 250))
        self.assertLess(power_fraction(b['rain-glass'], 20, 400), .1)
        self.assertGreater(power_fraction(b['rain-glass'], 1000, 9000), .6)
        self.assertGreater(power_fraction(b['cafe-murmur'], 150, 3500), .75)


class DefiningStructure(unittest.TestCase):
    def test_rain_is_dense_varied_droplets(self):
        rate, heights = onsets(beds()['rain-glass'])
        self.assertGreater(rate, 15, rate)
        self.assertGreater(np.percentile(heights, 90) / np.percentile(heights, 50), 1.35)
        self.assertGreater(heights.max(), 6, 'no clearly larger drops')
        for name, x in controls().items():
            self.assertLess(onsets(x)[0], 1, name)

    def test_rain_has_pitch_variety(self):
        x = bandpass(beds()['rain-glass'], 800, 9000)
        _, heights = onsets(beds()['rain-glass'])
        # spectral centroid of 20 ms frames is a proxy for droplet pitch; a noise bed has a steady one
        n = int(.02 * R)
        frames = np.lib.stride_tricks.sliding_window_view(x, n)[::n]
        S = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
        f = np.fft.rfftfreq(n, 1 / R)
        centroid = (S * f).sum(1) / (S.sum(1) + 1e-18)
        noise = bandpass(controls()['white'], 800, 9000)
        nf = np.lib.stride_tricks.sliding_window_view(noise, n)[::n]
        NS = np.abs(np.fft.rfft(nf * np.hanning(n), axis=1)) ** 2
        ncentroid = (NS * f).sum(1) / (NS.sum(1) + 1e-18)
        self.assertGreater(np.std(np.log(centroid)), 1.8 * np.std(np.log(ncentroid)))

    def test_fire_has_sparse_sharp_crackles_of_mixed_size_over_a_low_roar(self):
        x = beds()['fireplace']
        rate, heights = onsets(x)
        self.assertGreater(rate, 4, rate)
        self.assertLess(rate, 25, rate)
        self.assertGreater(np.percentile(heights, 50), 4, 'crackles must stand well above the roar')
        self.assertGreater(np.percentile(heights, 90) / np.percentile(heights, 50), 1.8, 'sizes must vary')
        self.assertGreater(heights.max(), 15, 'no pops')
        roar = envelope(bandpass(x, 20, 250), .5)[::480]
        self.assertGreater(np.percentile(roar, 10), .4 * np.median(roar), 'the roar must be a steady bed under the crackles')
        self.assertGreater(power_fraction(x, 20, 250), .1)
        # occasional hiss: 5-10 kHz envelope swells above its median at times
        hiss = envelope(bandpass(x, 5000, 10000), .1)
        self.assertGreater(np.percentile(hiss, 99) / np.median(hiss), 1.5)
        for name, c in controls().items():
            self.assertLess(onsets(c)[0], 1, name)

    def test_rain_is_denser_than_fire_and_fire_is_sharper_than_rain(self):
        rr, rh = onsets(beds()['rain-glass'])
        fr, fh = onsets(beds()['fireplace'])
        self.assertGreater(rr, 1.8 * fr)
        self.assertGreater(np.percentile(fh, 50), 1.8 * np.percentile(rh, 50))

    def test_thunder_is_a_few_long_low_rolls_with_a_huge_dynamic_range(self):
        count, dynamic = low_events(beds()['distant-thunder'])
        self.assertGreaterEqual(count, 3)
        self.assertLessEqual(count, 9)
        self.assertGreater(dynamic, 30, dynamic)
        for name, c in controls().items():
            n, d = low_events(c)
            self.assertLess(d, 10, name)
            self.assertEqual(n, 0, name) if name == 'white' else None

    def test_thunder_rolls_last_long_and_are_irregularly_spaced(self):
        e = envelope(bandpass(beds()['distant-thunder'], 20, 120), .3)[::480]
        sr = 50
        peak = np.argmax(e)
        floor = np.percentile(e, 10)
        # time the largest roll stays 12 dB above the quiet floor
        above = e > floor * 4
        run, best, here = 0, 0, 0
        for v in np.concatenate([above, above]):
            here = here + 1 if v else 0
            best = max(best, here)
        self.assertGreater(best / sr, 2.0, 'rolls should linger for seconds')
        # irregular: gaps between roll peaks are not equal
        e2 = e
        thr = 2 * np.median(e2)
        pk = [i for i in range(1, len(e2) - 1) if e2[i] >= e2[i - 1] and e2[i] > e2[i + 1] and e2[i] > thr]
        merged = []
        for i in pk:
            if merged and i - merged[-1] < 2 * sr:
                if e2[i] > e2[merged[-1]]:
                    merged[-1] = i
            else:
                merged.append(i)
        gaps = np.diff(merged)
        self.assertGreaterEqual(len(gaps), 2)
        self.assertGreater(np.std(gaps) / np.mean(gaps), .15)
        self.assertGreater(peak, -1)

    def test_thunder_sits_over_light_rain(self):
        # between rolls the high band is not silent but is far below the rain bed
        t, r = beds()['distant-thunder'], beds()['rain-glass']
        hit = envelope(bandpass(t, 1500, 9000), .5)
        hir = envelope(bandpass(r, 1500, 9000), .5)
        self.assertGreater(np.percentile(hit, 10), 0.0005)
        self.assertLess(np.median(hit), .5 * np.median(hir))

    def test_wind_is_slow_gusts_with_rustle_that_follows_them(self):
        depth, slow, corr = gust_modulation(beds()['forest-wind'])
        self.assertGreater(depth, 12, depth)
        self.assertGreater(slow, .6, slow)
        self.assertGreater(corr, .7, corr)
        self.assertGreater(rustle_depth(beds()['forest-wind']), 8, 'rustle band must rise and fall')
        for name, x in controls().items():
            d, s, c = gust_modulation(x)
            self.assertLess(d, 3, name)
            self.assertLess(abs(c), .5, name)
        rate, _ = onsets(beds()['forest-wind'], k=3.0)
        self.assertLess(rate, 3, 'wind is not a sequence of discrete events')

    def test_train_clacks_are_periodic_in_plausible_pairs(self):
        peak, lag, pair, pair_lag = clack_periodicity(beds()['train-carriage'])
        self.assertGreater(peak, .35, peak)
        self.assertGreater(lag, .5)
        self.assertLess(lag, 1.5)
        self.assertGreater(pair, .12, pair)
        self.assertGreater(pair_lag, .1)
        self.assertLess(pair_lag, .2)
        for name, x in controls().items():
            self.assertLess(clack_periodicity(x)[0], .15, name)
        for identity in IDS:
            if identity != 'train-carriage':
                self.assertLess(clack_periodicity(beds()[identity])[0], .25, identity)

    def test_train_has_sway(self):
        x = bandpass(beds()['train-carriage'], 30, 300)
        e = envelope(x, .5)[::480]
        a = e - e.mean()
        P = np.abs(np.fft.rfft(a)) ** 2
        f = np.fft.rfftfreq(len(a), 1 / 100)
        self.assertGreater(P[(f > .05) & (f < .5)].sum() / P[(f > .05)].sum(), .3)
        self.assertGreater(a.std() / e.mean(), .04)

    def test_cafe_has_voiced_syllabic_babble_clinks_and_no_rolls_or_periodic_clacks(self):
        x = beds()['cafe-murmur']
        self.assertGreater(voiced_fraction(x), .06)
        self.assertGreater(syllabic_share(x), .5)
        for name, c in controls().items():
            self.assertLess(voiced_fraction(c), .02, name)
            self.assertLess(syllabic_share(c), .4, name)
        # clinks: bright ringing events from 4 kHz
        rate, heights = onsets(x, lo=4000, k=2.0)
        self.assertGreater(rate, .8)
        self.assertGreater(heights.max(), 3)
        # babble has syllable-rate spectral movement
        self.assertLess(clack_periodicity(x)[0], .25)
        self.assertEqual(low_events(x)[0] <= 1, True)

    def test_cafe_is_unintelligible_no_pitch_above_speech_band_cheats(self):
        # 4+ overlapping talkers: the 300-3000 Hz band must never be dominated by a single strongly voiced frame train
        x = beds()['cafe-murmur']
        self.assertLess(voiced_fraction(x, .8), .02)


class MutualDistinctness(unittest.TestCase):
    def feature_vector(self, x):
        rate, h = onsets(x)
        depth, slow, corr = gust_modulation(x)
        return {
            'rate': rate, 'crest': float(np.percentile(h, 50)) if len(h) else 0.0,
            'lf_range': low_events(x)[1], 'depth': depth, 'rcorr': corr,
            'period': clack_periodicity(x)[0], 'voiced': voiced_fraction(x), 'syll': syllabic_share(x),
            'tilt': spectral_shape(x)[0],
        }

    def test_each_sound_owns_its_signature_feature(self):
        f = {i: self.feature_vector(beds()[i]) for i in IDS}
        top = lambda key, ident: self.assertEqual(max(IDS, key=lambda i: f[i][key]), ident, (key, {i: f[i][key] for i in IDS}))
        top('rate', 'rain-glass')
        top('crest', 'fireplace')
        top('lf_range', 'distant-thunder')
        top('period', 'train-carriage')
        top('voiced', 'cafe-murmur')
        top('syll', 'cafe-murmur')
        # wind: strongest slow modulation after thunder, and no events
        self.assertGreater(f['forest-wind']['depth'], max(f[i]['depth'] for i in ('rain-glass', 'cafe-murmur', 'train-carriage')) + 8)

    def test_pairwise_spectra_differ_by_at_least_4_db(self):
        spectra = {}
        for identity in IDS:
            c, d = third_octaves(beds()[identity])
            spectra[identity] = d - d.mean()
        for i, a in enumerate(IDS):
            for b in IDS[i + 1:]:
                diff = float(np.sqrt(np.mean((spectra[a] - spectra[b]) ** 2)))
                self.assertGreater(diff, 4.0, (a, b, diff))

    def test_leave_one_out_nearest_centroid_style_matching_uses_independent_slices(self):
        # Compare first-half and second-half signatures: each bed's halves must be nearer each other than any other bed.
        keys = ('tilt', 'rate_l', 'depth', 'voiced', 'period')

        def sig(x):
            rate, h = onsets(x)
            return np.array([spectral_shape(x)[0], np.log1p(rate), gust_modulation(x)[0], voiced_fraction(x) * 10,
                             clack_periodicity(x)[0] * 5])
        halves = {}
        for identity in IDS:
            x = beds()[identity]
            n = len(x) // 2
            halves[identity] = (sig(x[:n]), sig(x[n:]))
        for identity in IDS:
            own = np.linalg.norm(halves[identity][0] - halves[identity][1])
            for other in IDS:
                if other == identity:
                    continue
                cross = np.linalg.norm(halves[identity][0] - halves[other][1])
                self.assertLess(own, cross, (identity, other, own, cross, keys))


class SeamClippingLoudnessDeterminism(unittest.TestCase):
    def test_seam_has_no_click_and_the_oracle_can_see_one(self):
        for identity in IDS:
            with self.subTest(sound=identity):
                x = beds()[identity]
                self.assertLess(seam_excess(x), 1.2, seam_excess(x))
                self.assertLess(abs(x[0] - x[-1]), np.percentile(np.abs(np.diff(x)), 99), 'one-sample jump at the join')
        # sensitivity: a step at the join of noise-like beds must be flagged (the real beds contain transients at
        # least as sharp as a moderate step, so only their own-interior comparison above is meaningful for them)
        for name, x in controls().items():
            broken = x + np.linspace(0, .25 * np.abs(x).max(), len(x))
            self.assertLess(seam_excess(x), 1.2, name)
            self.assertGreater(seam_excess(broken), 1.2, name)
        for identity in ('rain-glass', 'distant-thunder'):
            x = beds()[identity]
            self.assertGreater(seam_excess(x + np.linspace(0, .25 * np.abs(x).max(), len(x))), 1.2, identity)

    def test_loop_join_level_is_continuous(self):
        # level change across the seam (100 ms RMS before vs after) must not be larger than the biggest such change elsewhere
        for identity in IDS:
            x = beds()[identity]
            n = len(x)
            tiled = np.concatenate([x, x])
            w = int(.1 * R)
            c = np.concatenate([[0], np.cumsum(tiled ** 2)])
            rms = lambda a, b: np.sqrt((c[b] - c[a]) / (b - a))
            jump = lambda at: abs(20 * np.log10((rms(at, at + w) + 1e-9) / (rms(at - w, at) + 1e-9)))
            seam = jump(n)
            others = max(jump(at) for at in range(w, n - w, w // 2))
            self.assertLessEqual(seam, others + 1.0, (identity, seam, others))

    def test_no_clipping_headroom_and_no_dc(self):
        for identity in IDS:
            x = beds()[identity]
            self.assertEqual(int((np.abs(x) >= 32767 / 32768).sum()), 0, identity)
            self.assertLess(np.abs(x).max(), .75, identity)
            self.assertLess(abs(x.mean()), 1e-2, identity)
            # peaks are limited, not hard clipped: no flat tops
            top = np.abs(x).max()
            self.assertLess(int((np.abs(x) > .995 * top).sum()) / len(x), 1e-2, identity)

    def test_no_dropouts_inside_the_loop(self):
        for identity in IDS:
            x = beds()[identity]
            one_second = np.sqrt(smooth(x * x, R))[R // 2::R // 2]
            if identity == 'distant-thunder':
                self.assertGreater(one_second.min(), 5e-4, 'light rain must never drop to digital silence')
            else:
                self.assertGreater(one_second.min(), .1 * one_second.mean(), identity)

    def test_loudness_is_matched_perceptually_with_raw_rms_headroom(self):
        # REVISION R5 (coordinator decision 2026-11-19). The six beds must be comparable to the ear, so they are matched
        # on A-weighted level (this file's a_weighted_level; BS.1770 was not chosen because its K-weighting is nearly
        # flat above 500 Hz and the helper already exists and is shared with the spread oracle), not on raw RMS.
        # ORIGINAL (superseded; cannot coexist with perceptual matching, because equal raw RMS left a 16.7 dB A-weighted
        # spread, distant-thunder 33.0 against rain-glass 49.8; reasons: operator feedback that the sounds should be
        # comparable to the ear, O7 acceptance finding 2, coordinator decision 2026-11-19):
        #     levels = {i: rms_db(beds()[i]) for i in IDS}
        #     target = 20 * np.log10(render.TARGET_RMS)
        #     for identity, level in levels.items():
        #         self.assertAlmostEqual(level, target, delta=1.0, msg=identity)
        #     self.assertLess(max(levels.values()) - min(levels.values()), 1.5)
        perceived = {i: a_weighted_level(beds()[i]) for i in IDS}
        median = float(np.median(list(perceived.values())))
        for identity, level in perceived.items():
            self.assertAlmostEqual(level, median, delta=1.5, msg=(identity, perceived))
        # headroom: matching perceived level must not push any bed's raw RMS near full scale
        for identity in IDS:
            self.assertLess(rms_db(beds()[identity]), -12.0, identity)

    def test_perceived_loudness_spread_is_bounded(self):
        # A-weighted level: the deep beds carry most of their RMS where hearing is insensitive. The spread is bounded so no
        # bed is much louder or much quieter to the ear than the rest at one volume setting.
        # R5: tightened from 20 dB to 3 dB, consistent with every bed within +-1.5 dB of the median.
        # ORIGINAL: self.assertLess(spread, 20, levels)
        levels = {i: a_weighted_level(beds()[i]) for i in IDS}
        spread = max(levels.values()) - min(levels.values())
        self.assertLessEqual(spread, 3.0, levels)

    def test_rain_under_thunder_is_audible_between_rolls(self):
        # O9-SOUND criterion 1 (acceptance finding 1): thunder's rain was about 30 dB below rain-glass in 1.5-9 kHz and its
        # 1 s RMS fell to about -55 dBFS between rolls.
        def band_db(x):
            f, P = welch(x)
            return float(10 * np.log10(P[(f >= 1500) & (f < 9000)].sum()))
        thunder, rain = beds()['distant-thunder'], beds()['rain-glass']
        gap = band_db(thunder) - band_db(rain)
        self.assertAlmostEqual(gap, 0.0, delta=10.0, msg='1.5-9 kHz level of thunder relative to rain-glass, dB: %.1f' % gap)
        one_second = np.sqrt(smooth(thunder * thunder, R))[R // 2::R // 2]
        self.assertGreater(20 * np.log10(one_second.min()), -40.0, 'thunder 1 s RMS must stay above -40 dBFS')

    def test_output_is_deterministic_across_processes_and_hash_seeds(self):
        script = ('import sys,hashlib; sys.path.insert(0, %r); import render_atmospheres as r; '
                  'print(*[hashlib.sha256(r.render(i).tobytes()).hexdigest() for i in %r])' % (str(SRC), IDS))
        outputs = []
        for seed in ('0', '12345'):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            outputs.append(subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True,
                                          check=True, cwd='/').stdout.split())
        self.assertEqual(outputs[0], outputs[1])
        manifest = {s['id']: s['sha256'] for s in beds()['manifest']['sounds']}
        for identity, digest in zip(IDS, outputs[0]):
            data = (Path(_cache['tmp'].name) / 'atmospheres' / (identity + '.wav')).read_bytes()
            self.assertEqual(manifest[identity], hashlib.sha256(data).hexdigest())
            self.assertEqual(digest, hashlib.sha256(render.render(identity).tobytes()).hexdigest())

    def test_catalogue_ids_files_license_and_provenance_are_unchanged(self):
        self.assertEqual(IDS, ['rain-glass', 'fireplace', 'distant-thunder', 'forest-wind', 'train-carriage', 'cafe-murmur'])
        manifest = beds()['manifest']
        self.assertEqual(manifest['license'], 'CC0-1.0')
        self.assertIn('procedural synthesis', manifest['provenance'])
        self.assertIn('no third-party samples', manifest['provenance'])
        self.assertEqual([s['file'] for s in manifest['sounds']], [i + '.wav' for i in IDS])
        written = json.loads((Path(_cache['tmp'].name) / 'atmospheres' / 'provenance.json').read_text())
        self.assertEqual(written['license'], 'CC0-1.0')
        for item in catalogue.SOUNDS:
            self.assertTrue(item['description'].strip())

    def test_short_renders_still_work_and_loop(self):
        # the existing suite renders short; the structure must scale down without raising or clipping
        for identity in IDS:
            x = np.array(render.render(identity, rate=8000, seconds=4), float) / 32768
            self.assertEqual(len(x), 32000)
            self.assertLess(np.abs(x).max(), .75)


if __name__ == '__main__':
    unittest.main()
