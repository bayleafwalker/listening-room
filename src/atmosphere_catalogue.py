"""Original, offline procedural sound beds packaged with Listening Room.

These are synthesized textures, never recordings of people or licensed samples.
"""
import os
from pathlib import Path

SOUNDS = (
    {'id': 'rain-glass', 'name': 'Rain on glass', 'outdoor': True,
     'description': 'Dense small droplets on sheltered glass, with a few larger drops and runnels over a soft wash.'},
    {'id': 'fireplace', 'name': 'Fireplace', 'outdoor': False,
     'description': 'A low warm roar with irregular crackles and pops, and an occasional hiss.'},
    {'id': 'distant-thunder', 'name': 'Distant thunder', 'outdoor': True,
     'description': 'Low, slow rumbles that roll and fade at irregular intervals over light rain.'},
    {'id': 'forest-wind', 'name': 'Forest wind', 'outdoor': True,
     'description': 'Slow gusts with leaf rustle that rises and falls with them; no bird calls or voices.'},
    {'id': 'train-carriage', 'name': 'Train carriage', 'outdoor': False,
     'description': 'A low carriage rumble with a gentle sway and rail-joint clacks in pairs.'},
    {'id': 'cafe-murmur', 'name': 'Café · nonverbal murmur', 'outdoor': False,
     'description': 'Synthetic, unintelligible babble with soft cup clinks and the odd chair movement; no recorded voices.'},
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
