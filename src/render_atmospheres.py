"""Render deterministic original sound textures. Generated WAVs: CC0-1.0.

Usage: python render_atmospheres.py ASSET_DIRECTORY
The textures repeat over forty seconds; all envelopes and events wrap around
the same circular buffer. No third-party recordings, speech or network access.
"""
from array import array
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import wave

from atmosphere_catalogue import SOUNDS

RATE = 24000
SECONDS = 40


def lowpass(samples, cutoff, rate):
    """Circular one-pole filter; warm up across the preceding loop boundary."""
    a = 1 - math.exp(-2 * math.pi * cutoff / rate)
    value = 0.0
    warmup = min(len(samples), max(32, int(rate * 10 / max(cutoff, .1))))
    for sample in samples[-warmup:]:
        value += a * (sample - value)
    result = array('f')
    for sample in samples:
        value += a * (sample - value)
        result.append(value)
    return result


def event(samples, start, duration, rng, rate, tone=None, strength=.05):
    """A wrapped, softly attacked event, silent at both envelope endpoints."""
    count = max(2, int(duration * rate))
    phase = rng.random() * math.tau
    for j in range(count):
        t = j / rate
        envelope = math.sin(math.pi * j / (count - 1)) ** 2
        carrier = rng.uniform(-1, 1) if tone is None else math.sin(math.tau * tone * t + phase)
        samples[(start + j) % len(samples)] += strength * envelope * carrier


def render(identity, rate=RATE, seconds=SECONDS):
    if identity not in {item['id'] for item in SOUNDS}:
        raise ValueError('Unknown atmosphere')
    if rate < 8000 or seconds < 2:
        raise ValueError('Sound beds need at least two seconds at 8 kHz')
    rng = random.Random(int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], 'big'))
    count = int(rate * seconds)
    white = array('f', (rng.uniform(-1, 1) for _ in range(count)))
    deep = lowpass(white, 85, rate)
    soft = lowpass(white, 1700, rate)
    hush = lowpass(white, 380, rate)
    samples = array('f')
    for i in range(count):
        phase = math.tau * i / count
        gust = .62 + .22 * math.sin(phase * 3 + .8) + .12 * math.sin(phase * 7)
        if identity == 'rain-glass':
            value = .20 * soft[i] + .035 * white[i] + .12 * hush[i]
        elif identity == 'fireplace':
            value = .32 * hush[i] + .025 * soft[i] + .20 * deep[i]
        elif identity == 'distant-thunder':
            rolls = (.5 + .5 * math.sin(phase * 2 - .7)) ** 5
            value = 1.4 * deep[i] * rolls + .06 * soft[i]
        elif identity == 'forest-wind':
            value = (.50 * hush[i] + .06 * soft[i]) * gust
        elif identity == 'train-carriage':
            value = .75 * deep[i] + .13 * hush[i] + .018 * soft[i]
        else:
            value = (.20 * hush[i] + .07 * soft[i]) * gust
        samples.append(value)
    if identity == 'rain-glass':
        for _ in range(max(1, int(seconds * 2))):
            event(samples, rng.randrange(count), rng.uniform(.02, .055), rng, rate,
                  tone=rng.uniform(1200, 2600), strength=rng.uniform(.004, .02))
    elif identity == 'fireplace':
        for _ in range(max(1, int(seconds * 1.8))):
            event(samples, rng.randrange(count), rng.uniform(.008, .05), rng, rate,
                  strength=rng.uniform(.025, .10))
    elif identity == 'train-carriage':
        # An integer number of rail-joint beats makes the rhythm loop naturally.
        beats = max(1, round(seconds / .85))
        for beat in range(beats):
            event(samples, int(count * beat / beats), .07, rng, rate, tone=95, strength=.035)
            event(samples, (int(count * beat / beats) + int(rate * .11)) % count,
                  .055, rng, rate, tone=130, strength=.018)
    elif identity == 'cafe-murmur':
        for _ in range(max(1, int(seconds / 3))):
            event(samples, rng.randrange(count), .10, rng, rate,
                  tone=rng.uniform(900, 1800), strength=.014)
    # Remove DC, set consistent modest average energy, and keep generous peaks.
    mean = sum(samples) / count
    rms = math.sqrt(sum((x - mean) ** 2 for x in samples) / count)
    peak = max(abs(x - mean) for x in samples)
    gain = min(.10 / max(rms, 1e-9), .65 / max(peak, 1e-9))
    return array('h', (round((x - mean) * gain * 32767) for x in samples))


def write_assets(directory, rate=RATE, seconds=SECONDS):
    directory = Path(directory) / 'atmospheres'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'license': 'CC0-1.0', 'source': 'render_atmospheres.py',
                'provenance': 'Original deterministic procedural synthesis; no third-party samples or recorded voices',
                'sample_rate': rate, 'seconds': seconds, 'sounds': []}
    for item in SOUNDS:
        path = directory / (item['id'] + '.wav')
        samples = render(item['id'], rate, seconds)
        if sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(samples.tobytes())
        manifest['sounds'].append(dict(item, file=path.name,
                                       sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    (directory / 'provenance.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    args = parser.parse_args()
    write_assets(args.directory)
