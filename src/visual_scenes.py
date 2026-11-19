"""Offline visual identity and restrained motion for each listening room.

Effect rectangles use normalized image coordinates, so they stay aligned when
rendering smaller previews. All motion is confined to the specified rectangles.
"""

SCENES = {
    'room-midnight-library': {
        'title': 'Midnight Library', 'accent': '#c79b68',
        'description': 'A quiet library after dark, lit for reading.',
        'motion': 'Fine rain moves across the window.',
        'audio_tone': 'Warm, intimate and contemplative.',
        'plate': 'midnight-library.jpg',
        'effects': [{'kind': 'rain', 'rect': (0.64, 0.08, 0.71, 0.53)}],
    },
    'room-night-drive': {
        'title': 'Night Drive', 'accent': '#76a9bd',
        'description': 'A quiet roadside stop after rain.',
        'motion': 'Wet-road reflections glimmer softly.',
        'audio_tone': 'Broad pop and presenters for the road; programming varies.',
        'plate': 'night-drive.jpg',
        'effects': [{'kind': 'glint', 'rect': (0.42, 0.70, 0.75, 0.85)}],
    },
    'room-northern-quiet': {
        'title': 'Northern Quiet', 'accent': '#9cb8b8',
        'description': 'Still northern water in the blue hour.',
        'motion': 'Low mist drifts over the water.',
        'audio_tone': 'Open, cool and unhurried.',
        'plate': 'northern-quiet.jpg',
        'effects': [{'kind': 'mist', 'rect': (0.24, 0.44, 0.65, 0.49)}],
    },
    'room-velvet-circuit': {
        'title': 'Velvet Circuit', 'accent': '#d6b671',
        'description': 'A softly lit electronic listening room.',
        'motion': 'A lamp glows gently in the corner.',
        'audio_tone': 'Brass and vintage swing with an electronic pulse; programming varies.',
        'plate': 'velvet-circuit.jpg',
        'effects': [{'kind': 'glow', 'rect': (0.48, 0.41, 0.55, 0.53)}],
    },
    'room-after-hours': {
        'title': 'After Hours', 'accent': '#c5a5df',
        'description': 'A rooftop looking out on the late city.',
        'motion': 'Rain-wet reflections glimmer on the rooftop.',
        'audio_tone': 'Bright, fast electronic music and pop; programming varies.',
        'plate': 'after-hours.jpg',
        'effects': [{'kind': 'glint', 'rect': (0.35, 0.78, 0.68, 0.90)}],
    },
    'room-atomic-lounge': {
        'title': 'Atomic Lounge', 'accent': '#e4a767',
        'description': 'A retro lounge with a warm evening glow.',
        'motion': 'A small lamp breathes with a slow warm glow.',
        'audio_tone': 'Playful, vintage and easygoing.',
        'plate': 'atomic-lounge.jpg',
        'effects': [{'kind': 'glow', 'rect': (0.20, 0.04, 0.33, 0.20)}],
    },
    'room-finnish-discoveries': {
        'title': 'Finnish Discoveries', 'accent': '#a9b7a2',
        'description': 'A Nordic sauna retreat for finding something new.',
        'motion': 'Low mist drifts over the water.',
        'audio_tone': 'Curious, clear and quietly eclectic.',
        'plate': 'finnish-discoveries.jpg',
        'effects': [{'kind': 'mist', 'rect': (0.68, 0.60, 0.90, 0.65)}],
    },
    'room-wizard-valley': {
        'title': 'Wizarding Valley', 'accent': '#b6ad85',
        'description': 'A distant enchanted valley seen from a sheltered retreat.',
        'motion': 'Faint mist drifts across the distant valley.',
        'audio_tone': 'Gentle celesta, harp and chamber strings; fantasy radio can become dramatic.',
        'plate': 'wizard-valley.png',
        'effects': [{'kind': 'mist', 'rect': (0.55, 0.45, 0.64, 0.49)}],
    },
}


# Effect parameters. Every key an effect may carry, with its type and range.
# A typo, a stray key or a value out of range fails the load (and so the
# asset build) with a message naming the file, the effect and the parameter.
def _number(low, high):
    return ('number', low, high)

def _whole(low, high):
    return ('whole', low, high)

_COMMON = {
    'kind': ('kind',), 'rect': ('rect',), 'polygon': ('polygon',), 'protect': ('polygons',),
    'feather': _number(0, 1), 'seed': _whole(0, 2**31), 'strength': _number(0, 1),
    'cycles': _whole(1, 64), 'color': ('colour',),
}
_PARAMETERS = {
    'rain': {'count': _whole(1, 2000), 'rain_cycles': ('cycle list',)},
    'mist': {}, 'steam': {}, 'glow': {}, 'glint': {'count': _whole(1, 2000)},
    'water': {'count': _whole(1, 2000), 'amplitude': _number(0, 30)},
    'shimmer': {'threshold': _whole(0, 255)},
    'electrical': {'threshold': _whole(0, 255)},
    'candle': {'threshold': _whole(0, 255), 'depth': _number(0, 4)},
    'cloud': {'amplitude': _number(0, 30), 'source_threshold': _whole(0, 255),
              'drift': _whole(-1, 1)},
    'smoke': {'amplitude': _number(0, 30), 'source_threshold': _whole(0, 255),
              'wisp': _number(4, 200), 'drift': _whole(-4, 4)},
    'fire': {'flame_threshold': _whole(0, 255), 'rise': _whole(1, 64), 'reach': _number(0, 1),
             'bed': _number(0, 1), 'suppress': _number(0, 1), 'sparks': _whole(0, 64),
             'spill': ('polygons',), 'spill_feather': _number(0, 1), 'spill_gain': _number(0, 2),
             'tongue': _number(1, 64), 'heat': _number(.1, 6), 'grain': _number(0, 1),
             'faint': _whole(0, 255), 'fill_radius': _number(1, 40), 'fill_gap': _whole(3, 15)},
}
_RIGID = {'name': ('text',), 'polygon': ('polygon',), 'darker_than': _whole(1, 255), 'grow': _whole(0, 4)}
_TOP = ('motion', 'effects', 'rigid')


def _check_value(where, name, spec, value):
    kind = spec[0]
    def fail(expected):
        raise ValueError('%s: %s must be %s, not %r' % (where, name, expected, value))
    def point(p):
        return (isinstance(p, (list, tuple)) and len(p) == 2 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in p))
    def polygon(p):
        return isinstance(p, (list, tuple)) and len(p) >= 3 and all(point(q) for q in p)
    if kind == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not spec[1] <= value <= spec[2]:
            fail('a number from %s to %s' % spec[1:])
    elif kind == 'whole':
        if isinstance(value, bool) or not isinstance(value, int) or not spec[1] <= value <= spec[2]:
            fail('a whole number from %s to %s' % spec[1:])
        if name == 'fill_gap' and value % 2 == 0:
            fail('an odd whole number from 3 to 15')
    elif kind == 'rect':
        if not (isinstance(value, (list, tuple)) and len(value) == 4 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value) and
                0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1):
            fail('four numbers [left, top, right, bottom] inside 0 to 1')
    elif kind == 'polygon':
        if not polygon(value):
            fail('a polygon of at least three [x, y] points inside 0 to 1')
    elif kind == 'polygons':
        if not (isinstance(value, (list, tuple)) and all(polygon(p) for p in value)):
            fail('a list of polygons, each of at least three [x, y] points inside 0 to 1')
    elif kind == 'colour':
        if not (isinstance(value, (list, tuple)) and len(value) == 3 and
                all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value)):
            fail('three whole numbers from 0 to 255')
    elif kind == 'cycle list':
        if not (isinstance(value, (list, tuple)) and value and
                all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 64 for v in value)):
            fail('a non-empty list of whole numbers from 1 to 64')
    elif kind == 'text':
        if not isinstance(value, str) or not value:
            fail('a non-empty text')
    elif kind == 'kind':
        if value not in _PARAMETERS:
            fail('one of ' + ', '.join(sorted(_PARAMETERS)))


def validate_tuning(filename, tuning):
    """Reject unknown keys and out-of-range values in a scene tuning file."""
    if not isinstance(tuning, dict):
        raise ValueError('%s: expected an object' % filename)
    for key in tuning:
        if key not in _TOP:
            raise ValueError('%s: unknown key %r (allowed: %s)' % (filename, key, ', '.join(_TOP)))
    if 'motion' in tuning and not isinstance(tuning['motion'], str):
        raise ValueError('%s: motion must be text' % filename)
    for kind, items, table in (('effect', tuning.get('effects', []), None), ('rigid region', tuning.get('rigid', []), _RIGID)):
        if not isinstance(items, list):
            raise ValueError('%s: %s must be a list' % (filename, 'effects' if table is None else 'rigid'))
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError('%s: %s %d must be an object' % (filename, kind, index))
            if table is None:
                effect = item.get('kind')
                if effect not in _PARAMETERS:
                    raise ValueError('%s: effect %d has unknown kind %r (known: %s)' % (
                        filename, index, effect, ', '.join(sorted(_PARAMETERS))))
                allowed = dict(_COMMON, **_PARAMETERS[effect])
                where = '%s: effect %d (%s)' % (filename, index, effect)
                if 'rect' not in item:
                    raise ValueError(where + ': missing rect')
            else:
                allowed = table
                where = '%s: rigid region %d (%s)' % (filename, index, item.get('name', 'unnamed'))
                if 'name' not in item or 'polygon' not in item:
                    raise ValueError(where + ': needs a name and a polygon')
            for name, value in item.items():
                if name not in allowed:
                    raise ValueError('%s: unknown parameter %r (allowed: %s)' % (
                        where, name, ', '.join(sorted(allowed))))
                _check_value(where, name, allowed[name], value)


# Scene sessions own separate files, so one room can be tuned and reviewed
# without overwriting another room's configuration. Values are ordinary data.
import json
from pathlib import Path
for _identity, _scene in SCENES.items():
    _path = Path(__file__).parent / 'scene_tuning' / (_identity.removeprefix('room-') + '.json')
    if _path.is_file():
        _tuning = json.loads(_path.read_text())
        validate_tuning(_path.name, _tuning)
        for _key in ('motion', 'effects', 'rigid'):
            if _key in _tuning:
                _scene[_key] = _tuning[_key]
