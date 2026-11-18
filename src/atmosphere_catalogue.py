"""Original, offline procedural sound beds packaged with Listening Room.

These are synthesized textures, never recordings of people or licensed samples.
"""
import os
from pathlib import Path

SOUNDS = (
    {'id': 'rain-glass', 'name': 'Rain on glass', 'outdoor': True,
     'description': 'A steady soft shower with scattered drops on sheltered glass.'},
    {'id': 'fireplace', 'name': 'Fireplace', 'outdoor': False,
     'description': 'A low fire with scattered, restrained crackles.'},
    {'id': 'distant-thunder', 'name': 'Distant thunder', 'outdoor': True,
     'description': 'Deep, slow thunder rolls behind a light rain texture.'},
    {'id': 'forest-wind', 'name': 'Forest wind', 'outdoor': True,
     'description': 'Slow gusts through leaves, without bird calls or voices.'},
    {'id': 'train-carriage', 'name': 'Train carriage', 'outdoor': False,
     'description': 'A muffled carriage rumble and softly repeating rail joints.'},
    {'id': 'cafe-murmur', 'name': 'Café · nonverbal murmur', 'outdoor': False,
     'description': 'Synthetic room rustle and soft cup-like clinks; no recorded voices.'},
)


def asset_directory():
    return Path(os.environ.get('LISTENING_HUB_ASSETS', Path(__file__).parent / 'assets')) / 'atmospheres'


def presets():
    return [dict(item, kind='local', source_id=item['id'],
                 url=str(asset_directory() / (item['id'] + '.wav')),
                 available=(asset_directory() / (item['id'] + '.wav')).is_file(),
                 license='CC0-1.0', provenance='Original procedural synthesis; no sampled recordings')
            for item in SOUNDS]


def source(identity):
    item = next((item for item in presets() if item['id'] == identity), None)
    if item is None:
        raise ValueError('Choose an atmosphere from the offline collection')
    if not item['available']:
        raise ValueError('This offline atmosphere is unavailable; reinstall Listening Room')
    return item
