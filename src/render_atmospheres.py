"""Render deterministic original sound textures. Generated WAVs: CC0-1.0.

Usage: python render_atmospheres.py ASSET_DIRECTORY
The textures repeat over forty seconds; all envelopes, filters and events wrap
around the same circular buffer. No third-party recordings, speech or network
access. Pure standard library, so the asset build needs no extra packages.

Each sound is built from the events that define it rather than from a filtered
noise bed: rain is thousands of small droplet ringings over a soft wash, a fire
is a low roar with clustered crackles and pops, thunder is long rolling rumbles
over light rain, wind is gust envelopes driving band-limited leaf rustle, the
train is a sway-modulated rumble with paired rail-joint clacks, and the cafe is
formant-shaped nonverbal babble with cup clinks and chair movement.

Level target: every bed is scaled to the same A-weighted RMS (A_TARGET_DB, the
measure of how loud a sound seems on ordinary speakers, not raw RMS, which
overrates deep rumbles), then peaks are softly limited below 0.65 of full scale.
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
A_TARGET_DB = -31.0  # A-weighted RMS every bed is matched to (dB re full scale); low enough that thunder's raw RMS stays below -12 dBFS
TARGET_RMS = 10 ** (A_TARGET_DB / 20)  # kept for callers: the same target as a linear A-weighted RMS
PEAK_CEILING = .65
CONTROL_RATE = 100


def silence(count):
    return array('f', bytes(4 * count))


def noise(rng, count):
    return array('f', (rng.uniform(-1, 1) for _ in range(count)))


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


def biquad(samples, kind, freq, q, rate):
    """Circular two-pole filter ('lp', 'hp' or 'bp'), primed across the loop seam."""
    freq = min(freq, .45 * rate)
    w = math.tau * freq / rate
    cw, alpha = math.cos(w), math.sin(w) / (2 * q)
    if kind == 'bp':
        b0, b1, b2 = alpha, 0.0, -alpha
    elif kind == 'lp':
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    else:
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
    a0 = 1 + alpha
    b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
    a1, a2 = -2 * cw / a0, (1 - alpha) / a0
    x1 = x2 = y1 = y2 = 0.0
    count = len(samples)
    warm = min(count, int(rate * (.05 + 12 * q / (math.pi * freq))))
    for x in samples[count - warm:]:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
    out = array('f')
    append = out.append
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        append(y)
    return out


def ring(buf, start, freq, tau, amp, rate, phase=0.0):
    """A wrapped damped sinusoid (a droplet, clink or thump), exact by recurrence."""
    n = len(buf)
    count = min(n, max(4, int(tau * 6 * rate)))
    r = math.exp(-1 / (tau * rate))
    w = math.tau * min(freq, .45 * rate) / rate
    c, r2 = 2 * r * math.cos(w), r * r
    y1, y2 = amp * math.sin(phase), amp * math.sin(phase - w) / r
    ramp = max(2, int(.0003 * rate))
    for j in range(count):
        if j:
            y = c * y1 - r2 * y2
            y2, y1 = y1, y
        buf[(start + j) % n] += y1 * (min(1.0, (j + 1) / ramp) if j < ramp else 1.0)


def burst(buf, start, length, amp, rng):
    """A short wrapped noise snap with a squared linear decay."""
    n = len(buf)
    length = max(2, length)
    for j in range(length):
        buf[(start + j) % n] += amp * rng.uniform(-1, 1) * (1 - j / length) ** 2


def scaled(rng, count, knots, lo, hi, power=1.0):
    """Circular smooth random control curve across the whole loop, `knots` points."""
    knots = max(2, int(knots))
    values = [lo + (hi - lo) * rng.random() ** power for _ in range(knots)]
    out = array('f')
    for i in range(count):
        pos = i * knots / count
        a = int(pos)
        f = pos - a
        f = f * f * (3 - 2 * f)
        out.append(values[a] + (values[(a + 1) % knots] - values[a]) * f)
    return out


def upsample(control, count):
    """Circular linear interpolation of a control-rate curve onto the sample grid."""
    k = len(control)
    out = array('f')
    for i in range(count):
        pos = i * k / count
        a = int(pos)
        f = pos - a
        out.append(control[a] + (control[(a + 1) % k] - control[a]) * f)
    return out


def control_length(seconds):
    return max(8, int(seconds * CONTROL_RATE))


def add_bump(curve, center_seconds, rise, fall, amp, tail=2.0):
    """Cosine rise, then a cosine (tail=0) or exponential-ish (tail>0) fall, wrapped."""
    k = len(curve)
    rise_n, fall_n = max(1, int(rise * CONTROL_RATE)), max(1, int(fall * CONTROL_RATE))
    center = int(center_seconds * CONTROL_RATE)
    for j in range(-rise_n, fall_n):
        if j < 0:
            shape = .5 - .5 * math.cos(math.pi * (j + rise_n) / rise_n)
        elif tail:
            shape = math.exp(-tail * j / fall_n) * (.5 + .5 * math.cos(math.pi * j / fall_n))
        else:
            shape = .5 + .5 * math.cos(math.pi * j / fall_n)
        curve[(center + j) % k] += amp * shape


def multiply(a, b, power=1.0):
    if power == 1.0:
        return array('f', (x * y for x, y in zip(a, b)))
    return array('f', (x * abs(y) ** power for x, y in zip(a, b)))


def mix(count, *layers):
    out = silence(count)
    for weight, layer in layers:
        for i in range(count):
            out[i] += weight * layer[i]
    return out


def tscale(seconds):
    return min(1.0, seconds / SECONDS)


def rain_layer(rng, count, rate, seconds, density=1.0, level=1.0, wash_level=.06, spread=.75):
    """Wash, dense small droplets with varied pitch and level, plinks and runnels."""
    white = noise(rng, count)
    wash = mix(count, (.55, biquad(white, 'bp', 3000, .4, rate)),
               (.12, biquad(white, 'lp', 700, .7, rate)))
    wash = multiply(wash, scaled(rng, count, seconds * .5, .8, 1.15))
    out = silence(count)
    for _ in range(max(2, int(seconds * 125 * density))):
        ring(out, rng.randrange(count), math.exp(rng.uniform(math.log(1900), math.log(6500))),
             rng.uniform(.0012, .0042), .035 * math.exp(rng.gauss(0, spread)), rate)
    for _ in range(max(1, int(seconds * 7 * density))):
        ring(out, rng.randrange(count), rng.uniform(1100, 2800), rng.uniform(.006, .014),
             .05 * math.exp(rng.gauss(0, .4)), rate)
    for _ in range(max(1, int(seconds * .55 * density))):
        start = rng.randrange(count)
        ring(out, start, rng.uniform(550, 1100), rng.uniform(.025, .045),
             .065 * math.exp(rng.gauss(0, .3)), rate)
        ring(out, start, rng.uniform(2800, 4200), .004, .12, rate)
    runnels = silence(control_length(seconds))
    for _ in range(max(1, int(seconds / 4.5 * density))):
        add_bump(runnels, rng.uniform(0, seconds), rng.uniform(.25, .6),
                 rng.uniform(.5, 1.3), rng.uniform(.5, 1.0), tail=0)
    trickle = multiply(biquad(white, 'bp', 4800, 1.6, rate), upsample(runnels, count))
    return mix(count, (level * wash_level, wash), (level, out), (level * .11, trickle))


def fire(rng, count, rate, seconds):
    white = noise(rng, count)
    roar = mix(count, (.9, biquad(white, 'lp', 135, .7, rate)),
               (.35, biquad(white, 'bp', 380, .8, rate)))
    flicker = scaled(rng, count, seconds * 1.4, .55, 1.1)
    out = multiply(roar, flicker)
    hiss_env = silence(control_length(seconds))
    for _ in range(max(1, int(seconds / 7))):
        add_bump(hiss_env, rng.uniform(0, seconds), rng.uniform(.15, .4),
                 rng.uniform(.4, 1.0), rng.uniform(.5, 1.0), tail=0)
    hiss = multiply(biquad(white, 'hp', 4500, .7, rate), upsample(hiss_env, count))
    events = silence(count)
    for _ in range(max(1, int(seconds * 2.3))):
        cluster = rng.randrange(count)
        for _ in range(rng.randint(1, 7)):
            at = (cluster + int(rng.expovariate(1 / .05) * rate)) % count
            kind = rng.random()
            if kind < .78:
                burst(events, at, int(rng.uniform(.001, .005) * rate),
                      .13 * math.exp(rng.gauss(0, .85)), rng)
            elif kind < .97:
                ring(events, at, rng.uniform(700, 2400), rng.uniform(.002, .006),
                     .30 * math.exp(rng.gauss(0, .5)), rate, phase=1.2)
                burst(events, at, int(.003 * rate), .20, rng)
            else:
                ring(events, at, rng.uniform(240, 520), rng.uniform(.01, .02), .75, rate, phase=1.2)
                burst(events, at, int(.006 * rate), .35, rng)
    return mix(count, (.24, out), (.04, hiss), (1.0, events))


def thunder(rng, count, rate, seconds):
    scale = tscale(seconds)
    # Light rain that is clearly audible between the rolls: a few evenly sized droplets plus a wash that swells and
    # fades over seconds. Sharp or dense droplets would either hide a loop-seam click or make thunder sound like
    # the rain bed; the swells keep the rain below the rain-on-glass bed's typical level.
    rain = rain_layer(rng, count, rate, seconds, density=.15, level=.15, wash_level=0, spread=.3)
    wash = biquad(noise(rng, count), 'bp', 3200, .5, rate)
    swell = scaled(rng, count, seconds * .5, .1, 1.0, power=3.5)
    rain = mix(count, (1.0, rain), (.05, multiply(wash, swell)))
    white = noise(rng, count)
    sub = mix(count, (1.0, biquad(white, 'lp', 80, .8, rate)), (.8, biquad(white, 'bp', 48, 1.4, rate)))
    body = biquad(white, 'lp', 210, .7, rate)
    envelope = silence(control_length(seconds))
    rolls = max(2, round(seconds / 9.5))
    spacing = seconds / rolls
    for r in range(rolls):
        at = (r + .5 + rng.uniform(-.35, .35)) * spacing
        add_bump(envelope, at, rng.uniform(.3, .8) * scale, 3.5 * scale + rng.uniform(0, 1.5) * scale,
                 rng.uniform(.7, 1.0), tail=2.2)
        for _ in range(rng.randint(3, 5)):
            add_bump(envelope, at + rng.uniform(.5, 4.5) * scale, rng.uniform(.3, .7) * scale,
                     rng.uniform(1.4, 2.6) * scale, rng.uniform(.25, .6), tail=2.0)
    # a faint continuous rumble under the rolls, so the quiet between them is never silent
    env = array('f', (e + .036 for e in upsample(envelope, count)))
    rumble = multiply(sub, env)
    front = array('f', (b * e * e for b, e in zip(body, env)))
    return mix(count, (1.0, rain), (2.0, rumble), (.9, front))


def wind(rng, count, rate, seconds):
    scale = tscale(seconds)
    white = noise(rng, count)
    gusts = silence(control_length(seconds))
    n_gusts = max(2, round(seconds / 6))
    for k in range(n_gusts):
        add_bump(gusts, (k + rng.uniform(.1, .9)) * seconds / n_gusts,
                 rng.uniform(1.2, 2.6) * scale, rng.uniform(1.8, 3.4) * scale,
                 rng.uniform(.5, 1.0), tail=0)
    curve = array('f', (min(1.4, .10 + g) for g in gusts))
    gust = upsample(curve, count)
    howl = multiply(mix(count, (1.0, biquad(white, 'lp', 220, .7, rate)),
                        (.45, biquad(white, 'bp', 600, .5, rate))), gust)
    flutter = scaled(rng, count, seconds * 28, .15, 1.0, power=1.6)
    leaves = multiply(multiply(mix(count, (1.0, biquad(white, 'bp', 3300, .8, rate)),
                                   (.2, biquad(white, 'bp', 5600, 1.0, rate))), flutter),
                      gust, power=1.7)
    ticks = silence(count)
    wanted = int(seconds * 55)
    placed = 0
    while placed < wanted:
        at = rng.randrange(count)
        if rng.random() < gust[at] ** 1.5 / 1.4:
            burst(ticks, at, int(rng.uniform(.003, .008) * rate), .09 * math.exp(rng.gauss(0, .5)), rng)
        placed += 1
    ticks = biquad(ticks, 'hp', 2200, .7, rate)
    return mix(count, (1.0, howl), (.4, leaves), (.3, ticks))


def train(rng, count, rate, seconds):
    white = noise(rng, count)
    beats = max(1, round(seconds / .9))
    sway_a = max(1, round(.21 * seconds))
    sway_b = max(1, round(.13 * seconds))
    sway = array('f', (1 + .22 * math.sin(math.tau * sway_a * i / count + 1.1)
                       + .12 * math.sin(math.tau * sway_b * i / count) for i in range(count)))
    rumble = multiply(mix(count, (1.0, biquad(white, 'lp', 115, .7, rate)),
                          (.8, biquad(white, 'bp', 58, 1.2, rate))), sway)
    hiss = biquad(white, 'bp', 1000, .5, rate)
    clacks = silence(count)
    period = count / beats
    for beat in range(beats):
        first = int(period * (beat + .5) + rng.uniform(-.004, .004) * rate) % count  # seam falls between clacks
        level = rng.uniform(.85, 1.1)
        for gap, weight, low in ((0.0, 1.0, 190), (.15, .72, 160)):
            at = (first + int(gap * rate)) % count
            ring(clacks, at, low, .03, .55 * level * weight, rate, phase=1.2)
            ring(clacks, at, low * 2.3, .016, .28 * level * weight, rate, phase=1.2)
            ring(clacks, at, 1350, .006, .16 * level * weight, rate, phase=1.2)
            burst(clacks, at, int(.008 * rate), .30 * level * weight, rng)
    return mix(count, (1.0, rumble), (.07, hiss), (1.0, clacks))


VOWELS = ((730, 1090), (530, 1840), (270, 2290), (570, 840), (300, 870), (660, 1720), (440, 1020))


def babble_voice(rng, buf, rate, seconds, level):
    """One nonverbal talker: voiced syllables shaped by drifting vowel formants."""
    n = len(buf)
    base = rng.choice((rng.uniform(95, 140), rng.uniform(170, 240)))
    t = rng.uniform(0, 2)
    while t < seconds:
        phrase_end = t + rng.uniform(.8, 2.4)
        phrase_level = level * rng.uniform(.5, 1.0)
        while t < phrase_end:
            length = int(rng.uniform(.11, .26) * rate)
            f1, f2 = rng.choice(VOWELS)
            f1, f2 = f1 * rng.uniform(.93, 1.07), f2 * rng.uniform(.93, 1.07)
            sections = []
            for freq, q in ((f1, 5.0), (f2, 7.0), (2650, 6.0)):
                w = math.tau * min(freq, .45 * rate) / rate
                alpha = math.sin(w) / (2 * q)
                a0 = 1 + alpha
                sections.append([alpha / a0, -alpha / a0, -2 * math.cos(w) / a0, (1 - alpha) / a0, 0.0, 0.0, 0.0, 0.0])
            start = int(t * rate)
            f0 = base * rng.uniform(.9, 1.12)
            glide = rng.uniform(-.08, .08)
            phase = rng.random()
            consonant = rng.random() < .4
            for j in range(length):
                phase += f0 * (1 + glide * (j / length - .5)) / rate
                phase -= int(phase)
                x = 2 * phase - 1
                total = 0.0
                for s, weight in zip(sections, (1.0, .55, .12)):
                    y = s[0] * x + s[1] * s[5] - s[2] * s[6] - s[3] * s[7]
                    s[5], s[4], s[7], s[6] = s[4], x, s[6], y
                    total += weight * y
                if consonant and j < int(.02 * rate):
                    total += .5 * rng.uniform(-1, 1) * (1 - j / (.02 * rate))
                buf[(start + j) % n] += phrase_level * total * math.sin(math.pi * j / length) ** 2
            t += length / rate + rng.uniform(.01, .06)
        t += rng.uniform(.4, 2.4)


def cafe(rng, count, rate, seconds):
    white = noise(rng, count)
    room = multiply(mix(count, (1.0, biquad(white, 'bp', 650, .35, rate)),
                        (.5, biquad(white, 'lp', 220, .7, rate))), scaled(rng, count, seconds * .4, .8, 1.2))
    talk = silence(count)
    for weight in (1.0, .55, .4, .3):
        babble_voice(rng, talk, rate, seconds, weight)
    talk = lowpass(lowpass(talk, 1800, rate), 3500, rate)
    clinks = silence(count)
    for _ in range(max(1, int(seconds / 4))):
        at = rng.randrange(count)
        base = rng.uniform(2300, 4200)
        for _ in range(rng.randint(1, 3)):
            tau = rng.uniform(.04, .11)
            for ratio, weight, decay in ((1.0, 1.0, 1.0), (2.32, .5, .6), (4.17, .25, .4)):
                ring(clinks, at, base * ratio * rng.uniform(.99, 1.01), tau * decay,
                     .11 * weight * rng.uniform(.7, 1.0), rate, phase=1.0)
            at = (at + int(rng.uniform(.08, .3) * rate)) % count
            base *= rng.uniform(.85, 1.2)
    moves = silence(count)
    for _ in range(max(1, int(seconds / 10))):
        at = rng.randrange(count)
        if rng.random() < .5:
            ring(moves, at, rng.uniform(70, 110), .06, .16, rate, phase=1.0)
            burst(moves, at, int(.05 * rate), .12, rng)
        else:
            length = int(rng.uniform(.35, .8) * rate)
            scrape = biquad(noise(rng, length), 'bp', rng.uniform(220, 380), 1.2, rate)
            for j in range(length):
                moves[(at + j) % count] += .35 * scrape[j] * math.sin(math.pi * j / length) ** 2
    return mix(count, (.15, room), (.7, talk), (.7, clinks), (1.0, moves))


VOICES = {'rain-glass': lambda rng, count, rate, seconds: rain_layer(rng, count, rate, seconds),
          'fireplace': fire, 'distant-thunder': thunder, 'forest-wind': wind,
          'train-carriage': train, 'cafe-murmur': cafe}


def a_weighted_rms(samples, rate=RATE):
    """A-weighted RMS (IEC 61672 curve, 0 dB at 1 kHz) via bilinear one-pole sections; linear in the input level."""
    def section(kind, freq):
        k = math.tan(math.pi * freq / rate)
        if kind == 'hp':
            return 1 / (1 + k), -1 / (1 + k), -(1 - k) / (1 + k)
        return k / (1 + k), k / (1 + k), -(1 - k) / (1 + k)
    stages = [section('hp', 20.6)] * 2 + [section('hp', 107.7), section('hp', 737.9)] + [section('lp', min(12194, .4 * rate))] * 2
    x = samples
    for b0, b1, a1 in stages:
        out = array('f', bytes(4 * len(x)))
        px = py = 0.0
        for i, v in enumerate(x):
            py = b0 * v + b1 * px - a1 * py
            px = v
            out[i] = py
        x = out
    # gain that makes the cascade 0 dB at 1 kHz
    w = 2 * math.pi * 1000 / rate
    z = complex(math.cos(w), -math.sin(w))
    h = 1.0
    for b0, b1, a1 in stages:
        h *= (b0 + b1 * z) / (1 + a1 * z)
    gain = 1 / abs(h)
    return gain * math.sqrt(sum(v * v for v in x) / len(x))


def normalise(samples):
    """Remove DC, set the A-weighted target level, then softly limit peaks below the ceiling."""
    count = len(samples)
    mean = sum(samples) / count
    centred = [x - mean for x in samples]
    knee = PEAK_CEILING * .78
    span = PEAK_CEILING - knee
    result = centred
    for _ in range(3):
        gain = TARGET_RMS / max(a_weighted_rms(result), 1e-9)
        result = [x * gain for x in result]
        result = [math.copysign(knee + span * math.tanh((abs(x) - knee) / span), x) if abs(x) > knee else x
                  for x in result]
    mean = sum(result) / count
    return array('h', (round((x - mean) * 32767) for x in result))


def render(identity, rate=RATE, seconds=SECONDS):
    if identity not in {item['id'] for item in SOUNDS}:
        raise ValueError('Unknown atmosphere')
    if rate < 8000 or seconds < 2:
        raise ValueError('Sound beds need at least two seconds at 8 kHz')
    rng = random.Random(int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], 'big'))
    return normalise(VOICES[identity](rng, int(rate * seconds), rate, seconds))


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
