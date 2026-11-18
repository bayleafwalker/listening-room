"""Rooms for the next window as plain data: cards, mood filters, Surprise me, the preview.

No GTK and no controller calls, so filtering, ordering, saved rooms and the
copy people read can be tested directly. Nothing here starts playback.
"""
import hashlib
import random

import listening_hub as hub

import next_model as model

MOODS = (('all', 'All'), ('yours', 'Yours'), ('focus', 'Focus'), ('reading', 'Reading'),
         ('late', 'Late night'), ('lively', 'Lively'), ('new', 'Something new'))
MOOD_WORDS = dict(MOODS)
# Where the scene sits, as a phrase after "Scene": the Workspace 9 name stays in Mix.
PLACE_PHRASES = {'9': 'on workspace 9', 'here': 'here', 'pin': 'on top', 'pip': 'small'}

# Authored traits for the catalogue rooms; tags below fill in rooms added later.
TRAITS = {
    'midnight-library': {'reading', 'late', 'focus'},
    'night-drive': {'late'},
    'northern-quiet': {'focus'},
    'velvet-circuit': {'lively'},
    'after-hours': {'late', 'lively'},
    'atomic-lounge': {'late'},
    'finnish-discoveries': {'new'},
    'wizard-valley': {'reading', 'focus'},
}
TAG_MOODS = {'focus': 'focus', 'ambient': 'focus', 'quiet': 'focus', 'reading': 'reading', 'dark academia': 'reading',
             'late': 'late', 'night': 'late', 'driving': 'late', 'energy': 'lively', 'electro swing': 'lively',
             'nightcore': 'lively', 'discovery': 'new'}
RECENT_WINDOW = 3  # Surprise me avoids the last few rooms you were in


def moods_of(room):
    found = set(TRAITS.get(room.get('id'), ()))
    for tag in room.get('tags') or ():
        if str(tag).lower() in TAG_MOODS:
            found.add(TAG_MOODS[str(tag).lower()])
    return found


def _ambience(recipe):
    atmosphere = (recipe or {}).get('atmosphere') or {}
    name = atmosphere.get('name')
    return name if atmosphere.get('kind', 'none') != 'none' and name else ''


def saved_entry(recipe, catalogue, sources):
    """A saved room (a controller variant) as a card. Its mood comes from the room it grew out of."""
    base = next((room for room in catalogue if room.get('id') == recipe.get('room_id')), None)
    source = next((item for item in sources if item.get('id') == (recipe.get('music') or {}).get('source_id')), {})
    lead = base['name'] if base else (recipe.get('music') or {}).get('name') or source.get('name') or ''
    ambience = _ambience(recipe)
    sub = ', with '.join(part for part in (lead, ambience) if part) if lead else ('With ' + ambience if ambience else 'Your room')
    return {'id': recipe['id'], 'kind': 'saved', 'name': recipe['name'], 'sub': sub,
            'poster': recipe.get('poster_path') or (base or {}).get('poster_path'), 'accent': recipe.get('accent') or (base or {}).get('accent'),
            'args': {'variant_id': recipe['id']}, 'yours': True, 'legacy': False, 'editable': True,
            'moods': sorted(moods_of(base) - {'new'}) if base else [], 'tags': list((base or {}).get('tags') or []),
            'description': ('Your own mix, saved from ' + base['name'] + '.') if base else 'Your own mix.',
            'music': (recipe.get('music') or {}).get('name') or source.get('name') or '',
            'music_kind': source.get('kind'), 'view_name': 'Scene', 'view_id': (recipe.get('picture') or {}).get('preset_id'),
            'scene_available': bool((recipe.get('picture') or {}).get('preset_id')),
            'scene_on': bool((recipe.get('picture') or {}).get('enabled')), 'ambience': ambience, 'recipe': recipe}


def legacy_scenes(scenes, sources, presets, catalogue=()):
    """Scenes saved in the classic window become saved rooms.

    Built-in scenes are skipped (the catalogue already has those rooms); so is any
    scene whose station is gone, since it could not be entered.
    """
    result = []
    for scene in scenes or ():
        if not isinstance(scene, dict) or not str(scene.get('id', '')).startswith('user-'):
            continue
        source = next((item for item in sources if item.get('id') == scene.get('source_id')), None)
        if source is None:
            continue
        preset = next((item for item in presets if item.get('id') == scene.get('ambience_id')), None)
        recipe = {'id': 'scene-' + scene['id'], 'room_id': None, 'name': scene['name'],
                  'poster_path': (preset or {}).get('poster_path'), 'accent': (preset or {}).get('accent'),
                  'music': {'source_id': source['id'], 'name': source['name'], 'volume': scene.get('volume', 50), 'muted': False},
                  'atmosphere': {'kind': 'none', 'volume': 20, 'muted': False},
                  'picture': {'preset_id': scene.get('ambience_id'), 'enabled': bool(preset),
                              'placement': scene.get('placement', '9'), 'still': False}}
        entry = saved_entry(recipe, catalogue, sources)
        entry.update(args={'recipe': recipe}, legacy=True, editable=False, sub=source['name'] + ' · from your scenes')
        result.append(entry)
    return result


def catalogue_entry(room):
    return {'id': room['id'], 'kind': 'room', 'name': room['name'], 'sub': room.get('subtitle', ''), 'poster': room.get('poster_path'),
            'accent': room.get('accent'), 'args': {'room_id': room['id']}, 'yours': False, 'legacy': False, 'editable': False,
            'moods': sorted(moods_of(room)), 'tags': list(room.get('tags') or []), 'description': room.get('description', ''),
            'music': room.get('source_name', ''), 'music_kind': room.get('source_kind'), 'view_name': room.get('view_name', 'Scene'),
            'view_id': room.get('view_id'), 'scene_available': bool(room.get('view_id')), 'scene_on': bool(room.get('view_id')), 'ambience': '', 'recipe': None}


def entries(snapshot, catalogue, sources=(), scenes=()):
    """Saved rooms first (variants, then legacy scenes with a new name), then the catalogue."""
    saved = [saved_entry(item, catalogue, sources) for item in (snapshot or {}).get('variants') or []]
    taken = {item['name'].lower() for item in saved}
    saved += [item for item in scenes if item['name'].lower() not in taken]
    return saved + [catalogue_entry(room) for room in catalogue if room.get('available', True)]


def _history(snapshot):
    snapshot = snapshot or {}
    recent = [item for item in snapshot.get('recent') or [] if isinstance(item, str)]
    last = (snapshot.get('last_session') or {}).get('id')
    return recent + ([last] if last and last not in recent else [])


def matches(entry, mood, snapshot=None):
    if mood == 'all':
        return True
    if mood == 'yours':
        return entry['yours']
    if mood == 'new':
        # Authored as new, or a catalogue room you have not been in yet once you have a history.
        history = _history(snapshot)
        return 'new' in entry['moods'] or bool(history and not entry['yours'] and entry['id'] not in history)
    return mood in entry['moods']


def filtered(items, mood, snapshot=None):
    return [item for item in items if matches(item, mood, snapshot)]


def surprise(items, mood, snapshot, rng=None):
    """A room to preview: from the chosen chip, not the room you are in or just left."""
    rng = rng or random
    pool = filtered(items, mood, snapshot)
    active = (snapshot or {}).get('active') or {}
    skip = [active.get('id')] + _history(snapshot)[:RECENT_WINDOW]
    for width in (len(skip), 1, 0):  # relax recent history first, then the room you are in
        fresh = [item for item in pool if item['id'] not in skip[:width]]
        if fresh:
            return rng.choice(fresh)
    return None


def enter_args(entry, scene_on=None):
    """The one command that starts a room. Saved rooms keep their own scene unless the toggle says otherwise."""
    args = dict(entry['args'])
    if scene_on is not None:
        args['with_view'] = bool(scene_on)
    elif entry['kind'] == 'room':
        args['with_view'] = True
    return args


def previous_room(snapshot, catalogue):
    """How to go back to the room being left: a saved room, a catalogue room, or its own recipe."""
    active = (snapshot or {}).get('active')
    if not active:
        return None
    if any(item.get('id') == active.get('id') for item in snapshot.get('variants') or []):
        args = {'variant_id': active['id']}
    elif any(room.get('id') == active.get('room_id') and room.get('available', True) for room in catalogue):
        args = {'room_id': active['room_id']}
    else:
        args = {'recipe': active}
    return {'name': active.get('name') or 'your room', 'args': args}


def is_active(entry, snapshot):
    active = (snapshot or {}).get('active') or {}
    return bool(active) and entry['id'] == active.get('id')


def music_detail(entry):
    kind = entry.get('music_kind')
    if kind == 'radio':
        return 'Live radio · programming varies'
    if kind in ('web', 'discovery'):
        return 'Opens in your browser'
    return ''


def preview(entry, snapshot):
    """Everything the Room preview shows, in the listener's words."""
    settings = (snapshot or {}).get('settings') or {}
    where = PLACE_PHRASES.get(settings.get('placement') or '9', 'on workspace 9')
    moving = not (settings.get('still') or settings.get('reduced_motion'))
    if not entry['scene_available']:
        scene = 'No scene for this room'
    elif entry['scene_on']:
        scene = 'Scene %s · %s' % (where, 'moving' if moving else 'still')
    else:
        scene = 'Scene off · sound only'
    active = (snapshot or {}).get('active') or {}
    inside = is_active(entry, snapshot)
    if inside:
        note = 'You are in this room now.'
    elif active:
        note = '%s fades out first. Other apps keep playing.' % (active.get('name') or 'Your current room')
    else:
        note = 'Nothing starts until you enter. Other apps keep playing.'
    return {'title': entry['name'], 'subtitle': entry['sub'], 'description': entry['description'],
            'music': entry['music'] or 'Music', 'music_detail': music_detail(entry), 'scene': scene,
            'scene_available': entry['scene_available'], 'scene_on': entry['scene_on'],
            'ambience': entry['ambience'] or 'No ambience',
            'ambience_detail': 'Rain, traffic and more from Mix once inside' if not entry['ambience'] else 'Plays with the room',
            'tags': [tag.capitalize() if tag.islower() else tag for tag in entry['tags']],
            'button': 'Open room' if inside else 'Enter room', 'inside': inside, 'note': note}


def save_lines(snapshot):
    """What Save as my room will keep, as (title, detail) pairs."""
    lines = []
    for row in model.mix_rows(snapshot):
        if row['closed']:
            continue
        detail = row['source']
        if row['layer'] == 'picture':
            detail = PLACE_PHRASES.get(row.get('placement'), 'on workspace 9') + (' · moving' if row.get('moving') else ' · still')
        lines.append((row['title'], detail))
    volume = snapshot.get('room_volume')
    if isinstance(volume, (int, float)):
        lines.append(('Room volume', '%d%%' % round(volume)))
    return lines


def variant_id(name):
    """The id the controller gives a room saved under this name."""
    return 'variant-' + hashlib.sha256(hub.clean(name).encode()).hexdigest()[:16]
