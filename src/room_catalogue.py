"""Room recipes compose existing sources and views; browsing has no effects."""
from pathlib import Path

import listening_hub as hub
import ambience
import audio_layers


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


def launch_room(room_id, with_view=True, still=False):
    room = next((item for item in rooms() if item['id'] == room_id), None)
    if room is None or not room['available']:
        raise hub.HubError('This room has no available sound source')
    source = next((item for item in hub.sources() if item['id'] == room['source_id']), None)
    view = next((item for item in ambience.presets() if item['id'] == room['view_id']), None)
    if with_view and view is None:
        raise hub.HubError('This view is unavailable. Choose sound only or another room.')
    if with_view and still:
        if not view.get('still_url') or not Path(view['still_url']).is_file():
            raise hub.HubError('Still artwork is unavailable for this view. Turn off Still artwork or choose another room.')
        view = dict(view, url=view['still_url'], name=view['name'] + ' · still artwork')
    hub.play(source)
    hub.update_state(last_room=room_id)
    browser = source['kind'] in ('web', 'discovery')
    message = ('Opened ' + source['name'] + '. Start playback in your browser.'
               if browser else 'Playing ' + source['name'] + '.')
    errors = []
    if with_view:
        try:
            ambience.start(view, mode='ambience', placement='9')
            message += ' The silent view is on workspace 9.'
        except (ValueError, OSError, hub.HubError) as error:
            message += ' The view could not open; use Views to try again.'
            errors.append(str(error))
    return {'message': message, 'room': room, 'errors': errors}


def quiet_room(target_id=None):
    infos = hub.all_info()
    # None means no external app was selected when the action was pressed;
    # never adopt a browser that appears while this action is queued.
    selected = next((item for item in infos if item['id'] == target_id), None) if target_id is not None else None
    targets = {item['id']: item for item in infos if item['id'] in ('radio', 'video')}
    if selected:
        targets[selected['id']] = selected
    errors, paused = [], []
    if target_id is not None and selected is None:
        errors.append('The selected player is no longer available')
    for identity, item in targets.items():
        if item['status'].lower() != 'playing':
            continue
        try:
            hub.player_command(identity, 'pause')
            paused.append(identity)
        except (hub.HubError, ValueError, OSError) as error:
            errors.append(str(error))
    if ambience.state('ambience'):
        try:
            ambience.control('pause', 'ambience')
            paused.append('ambience')
        except (ValueError, OSError) as error:
            errors.append(str(error))
    layer = audio_layers.state()
    if layer and layer.get('status', '').lower() == 'playing':
        try:
            audio_layers.control('pause')
            paused.append('overlay')
        except (ValueError, OSError) as error:
            errors.append(str(error))
    message = 'Room paused.' if paused else 'Nothing in the room is playing.'
    if errors:
        message += ' Some controls could not be reached.'
    return {'message': message, 'paused': paused, 'errors': errors}


def stop_room():
    stopped, errors = [], []
    for name in ('radio', 'ambience', 'video', 'overlay'):
        unit = hub.radio_unit() if name == 'radio' else 'listening-hub-' + name + '.service'
        try:
            if name == 'radio':hub.radio_audio_state()
            elif name == 'video':ambience._video_audio()
            elif name == 'overlay':audio_layers.remember_audio()
            result = hub.run(['systemctl', '--user', 'stop', unit], check=False)
            if result.returncode:
                loaded = hub.run(['systemctl', '--user', 'show', unit, '--property=LoadState', '--value'], check=False)
                if loaded.returncode or loaded.stdout.strip() != 'not-found':
                    errors.append('Could not stop ' + name)
            else:
                stopped.append(name)
        except (hub.HubError, ValueError, OSError) as error:
            errors.append(str(error))
    message = 'Room players stopped.' if not errors else 'Some room players could not be stopped.'
    return {'message': message, 'stopped': stopped, 'errors': errors}
