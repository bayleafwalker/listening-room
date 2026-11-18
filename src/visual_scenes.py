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


# Scene sessions own separate files, so one room can be tuned and reviewed
# without overwriting another room's configuration. Values are ordinary data.
import json
from pathlib import Path
for _identity, _scene in SCENES.items():
    _path = Path(__file__).parent / 'scene_tuning' / (_identity.removeprefix('room-') + '.json')
    if _path.is_file():
        _tuning = json.loads(_path.read_text())
        for _key in ('motion', 'effects', 'rigid'):
            if _key in _tuning:
                _scene[_key] = _tuning[_key]
