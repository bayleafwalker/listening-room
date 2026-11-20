"""Room recipes compose existing sources and views; browsing has no effects."""
from pathlib import Path

import ambience
import player_core as hub


RECIPES = (
    {'id': 'midnight-library', 'name': 'Midnight library',
     'subtitle': 'Piano by lamplight', 'source_id': 'dark-academia-piano',
     'view_ids': ('room-midnight-library', 'moonlit-lake'),
     'description': 'A book-lined study after midnight. Piano, amber lamplight and rain beyond the tall windows.',
     'tags': ['reading', 'dark academia', 'piano']},
    {'id': 'night-drive', 'name': 'Night drive',
     'subtitle': 'Radio for the road', 'source_id': 'truckersfm',
     'view_ids': ('room-night-drive', 'norway', 'northern-lake'),
     'description': 'A sheltered stop on a blue-hour motorway. TruckersFM, wet asphalt and distant road lights. Norway films are also in Views.',
     'tags': ['driving', 'presenters', 'landscapes']},
    {'id': 'northern-quiet', 'name': 'Northern quiet',
     'subtitle': 'Space to concentrate', 'source_id': 'somafm-drone-zone',
     'view_ids': ('room-northern-quiet', 'northern-lake'),
     'description': 'Slow ambient textures and a lake with moving mist. A steady background for a long stretch of work.',
     'tags': ['focus', 'ambient', 'quiet']},
    {'id': 'velvet-circuit', 'name': 'Velvet circuit',
     'subtitle': 'Swing with a pulse', 'source_id': 'lautfm-electro-swing',
     'view_ids': ('room-velvet-circuit', 'moonlit-lake'),
     'description': 'An intimate Art Deco listening lounge: brass, jade and burgundy. Electro swing brings the momentum.',
     'tags': ['electro swing', 'energy']},
    {'id': 'after-hours', 'name': 'After hours',
     'subtitle': 'Nightcore after dark', 'source_id': 'lautfm-nightcore',
     'view_ids': ('room-after-hours', 'moonlit-lake'),
     'description': 'A quiet rooftop above a violet city. Soft neon and rain outside; a brighter, faster nightcore soundtrack.',
     'tags': ['nightcore', 'energy', 'late']},
    {'id': 'atomic-lounge', 'name': 'Atomic lounge',
     'subtitle': 'A little retro escapism', 'source_id': 'somafm-secret-agent',
     'view_ids': ('room-atomic-lounge', 'moonlit-lake'),
     'description': 'A retrofuturist desert diner with a valve radio and a view of tomorrow. Cinematic lounge and a little retro-spy atmosphere.',
     'tags': ['atompunk', 'retro', 'lounge']},
    {'id': 'finnish-discoveries', 'name': 'Finnish discoveries',
     'subtitle': 'Follow a new sound', 'source_id': 'ylex',
     'view_ids': ('room-finnish-discoveries', 'northern-lake'),
     'description': 'A lakeside sauna at midsummer: birches, red timber and a quiet pier. YleX brings Finnish music and culture to the lakeside.',
     'tags': ['Finnish', 'discovery', 'YleX']},
    {'id': 'wizard-valley', 'name': 'Wizarding Valley',
     'subtitle': 'Fantasy radio', 'source_id': 'radio-rivendell',
     'view_ids': ('room-wizard-valley', 'wizard-artwork', 'moonlit-lake'),
     'description': 'Your original valley artwork, with a warm lantern in the sheltered study. Radio Rivendell supplies a fantasy soundtrack that can range from gentle to dramatic.',
     'tags': ['fantasy', 'wizarding', 'instrumental', 'reading']},
)


def _poster(view):
    if view:
        poster = view.get('poster_path')
        if poster and Path(poster).is_file():
            return str(poster)
        path = Path(view.get('url', ''))
        if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp') and path.is_file():
            return str(path)
        if path.is_absolute() and path.with_suffix('.png').is_file():
            return str(path.with_suffix('.png'))
    fallback = ambience.ASSETS / 'northern-lake.png'
    return str(fallback) if fallback.is_file() else None


def rooms():
    sources = {item['id']: item for item in hub.sources()}
    views = {item['id']: item for item in ambience.presets()}
    last = hub.state().get('last_room')
    result = []
    for recipe in RECIPES:
        source = sources.get(recipe['source_id'])
        view = next((views[key] for key in recipe['view_ids']
                     if key in views and views[key].get('available', True)), None)
        item = {key: value for key, value in recipe.items() if key != 'view_ids'}
        item.update(source_name=source['name'] if source else 'Source unavailable',
                    source_kind=source.get('kind') if source else None,
                    view_id=view['id'] if view else None,
                    view_name=view['name'] if view else 'View unavailable',
                    still_available=bool(view and view.get('still_url') and Path(view['still_url']).is_file()),
                    motion=view.get('motion', '') if view else '',
                    audio_tone=view.get('audio_tone', '') if view else '',
                    accent=view.get('accent', '') if view else '',
                    poster_path=_poster(view), available=source is not None,
                    last_entered=last == recipe['id'])
        result.append(item)
    return result
