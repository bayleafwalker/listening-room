"""View state for the next Listening Room window, derived from controller snapshots.

Everything here is plain data in, plain data out, so the vocabulary, launch
rules, capability rules and failure wording can be tested without GTK.
"""
import math
import re

MUSIC, AMBIENCE, SCENE = 'Music', 'Ambience', 'Scene'
# Controller layer names are an implementation detail; people see these words.
LAYER_WORDS = {'music': MUSIC, 'atmosphere': AMBIENCE, 'picture': SCENE}
PLACEMENTS = (('9', 'Workspace 9'), ('here', 'Here'), ('pin', 'On top'), ('pip', 'Small'))
NO_HYPRLAND = 'Placement needs Hyprland. In this session the scene shows in this window.'
DEFAULT_ACCENT = '#c79b68'
LAYER_ORDER = ('music', 'atmosphere', 'picture')
TIMER_WORDS = {'break': 'Break', 'wind_down': 'Wind down', 'dim': 'Dim the scene'}


def accent(value):
    return value if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else DEFAULT_ACCENT


def launch_view(snapshot):
    """Rooms unless a room is already active. Launching never starts playback."""
    return 'now' if (snapshot or {}).get('active') else 'rooms'


def hero(snapshot, catalogue=()):
    """What the Rooms page offers first: return to the last room, or pick one."""
    snapshot = snapshot or {}
    active, last = snapshot.get('active'), snapshot.get('last_session')
    room = active or last
    if not room:
        return {'kind': 'first-run', 'title': 'Pick a room to begin', 'detail': ''}
    entry = next((item for item in catalogue if item.get('id') == room.get('room_id')), {})
    ambience = room.get('atmosphere') or {}
    detail = entry.get('subtitle') or ''
    if ambience.get('kind', 'none') != 'none' and ambience.get('name'):
        detail = (detail + ', with ' if detail else 'With ') + ambience['name']
    return {'kind': 'now' if active else 'return', 'title': room.get('name') or 'Your listening room',
            'detail': detail, 'poster': room.get('poster_path') or entry.get('poster_path'),
            'accent': accent(room.get('accent') or entry.get('accent')),
            'when': None if active else snapshot.get('last_entered_at'),
            'button': ('Open ' if active else 'Return to ') + (room.get('name') or 'your room')}


def when_words(stamp, now):
    if not isinstance(stamp, (int, float)) or not isinstance(now, (int, float)):
        return 'Last time'
    days = int((now - stamp) // 86400)
    if days <= 0 and now - stamp < 4 * 3600:
        return 'Just now'
    return {0: 'Earlier today', 1: 'Yesterday'}.get(days, 'Last week' if days < 14 else 'A while ago')


def source_chip(active):
    music = (active or {}).get('music') or {}
    reference = music.get('reference') or {}
    if reference.get('provider') in ('jellyfin', 'music_assistant'):
        return 'Library'
    if music.get('source_id') in ('twitch-channel', 'youtube-video') or music.get('resolver'):
        return 'Live channel'
    if music.get('kind') == 'local':
        return 'Local sound'
    return 'Live radio'


def can_skip(snapshot):
    """Whether Next track exists: the same rule MPRIS uses (never live radio)."""
    import session_service
    return bool(session_service.mpris_values(snapshot)['CanGoNext'])


def can_feedback(snapshot):
    """More / Less like this exists only while a generated library station has played a track."""
    station = snapshot.get('station') or {}
    return bool(station.get('explanation') is not None and station.get('recent'))


def _layer(snapshot, name):
    return (snapshot.get('layers') or {}).get(name) or {}


def _playing(layer):
    return layer.get('status') == 'Playing'


def now_line(snapshot):
    active = snapshot.get('active') or {}
    music = _layer(snapshot, 'music')
    current = (snapshot.get('station') or {}).get('current') or {}
    title = current.get('name') or music.get('title') or ''
    other = current.get('artist') or music.get('artist') or ''
    source = music.get('station') or music.get('name') or (active.get('music') or {}).get('name') or ''
    primary = title or source or active.get('name') or ''
    secondary = other or (source if source and source != primary else '')
    return primary, secondary


def layer_summary(snapshot):
    """Names of what is sounding, for the Mix button: Piano · Rain on glass · Scene."""
    parts = []
    music, ambience, picture = (_layer(snapshot, name) for name in LAYER_ORDER)
    active = snapshot.get('active') or {}
    if music.get('status') in ('Playing', 'Paused'):
        parts.append(music.get('station') or music.get('name') or (active.get('music') or {}).get('name') or MUSIC)
    if ambience.get('status') in ('Playing', 'Paused') and ambience.get('kind', 'local') != 'none':
        parts.append(ambience.get('name') or ambience.get('title') or AMBIENCE)
    if picture.get('status') in ('Playing', 'Paused'):
        parts.append(SCENE)
    return ' · '.join(parts) if parts else 'Nothing playing'


def room_state(snapshot):
    """'playing' | 'paused' | 'idle' for the single play/pause control."""
    layers = [_layer(snapshot, name) for name in ('music', 'atmosphere')]
    if snapshot.get('resume_offer') or any(layer.get('status') == 'Paused' for layer in layers):
        return 'paused'
    return 'playing' if any(_playing(layer) for layer in layers) else 'idle'


def failure(snapshot):
    """One banner for the highest-priority failing layer, or None."""
    errors = snapshot.get('errors') or {}
    for name in LAYER_ORDER:
        if errors.get(name):
            break
    else:
        return None
    others = [LAYER_WORDS[other] for other in LAYER_ORDER
              if other != name and _playing(_layer(snapshot, other))]
    if not others:
        keeps = 'Nothing else is playing.'
    elif len(others) == 1:
        keeps = others[0] + ' keeps playing.'
    else:
        keeps = ' and '.join(others) + ' keep playing.'
    return {'layer': name, 'title': LAYER_WORDS[name] + ' couldn’t start', 'detail': keeps,
            'error': str(errors[name])}


def timers(snapshot):
    """Running timers, soonest first, with whole minutes remaining (rounded up)."""
    now = snapshot.get('now')
    result = []
    for kind, ends in (snapshot.get('timers') or {}).items():
        if not isinstance(ends, (int, float)) or not isinstance(now, (int, float)) or ends <= now:
            continue
        minutes = max(1, math.ceil((ends - now) / 60))
        result.append({'kind': kind, 'minutes': minutes, 'ends_at': ends,
                       'label': '%s · %d min' % (TIMER_WORDS.get(kind, kind), minutes)})
    return sorted(result, key=lambda item: item['ends_at'])


def now_model(snapshot):
    """Everything Now shows at rest. None when no room is active."""
    active = snapshot.get('active')
    if not active:
        return None
    primary, secondary = now_line(snapshot)
    return {'name': active.get('name') or 'Your listening room', 'accent': accent(active.get('accent')),
            'chip': source_chip(active), 'line': primary, 'detail': secondary,
            'summary': layer_summary(snapshot), 'state': room_state(snapshot),
            'can_next': can_skip(snapshot), 'can_feedback': can_feedback(snapshot), 'room_volume': snapshot.get('room_volume', 100),
            'failure': failure(snapshot), 'timers': timers(snapshot)}


def mix_rows(snapshot):
    """The three Mix rows with the capabilities each layer actually has."""
    active = snapshot.get('active') or {}
    layers, levels, errors = (snapshot.get(key) or {} for key in ('layers', 'levels', 'errors'))
    rows = []
    for name in LAYER_ORDER:
        layer, selection = layers.get(name) or {}, active.get(name) or {}
        status = layer.get('status', 'Stopped')
        closed = status == 'Stopped' or selection.get('kind') == 'none' or selection.get('enabled') is False
        failed = bool(errors.get(name))
        if name == 'music':
            source = layer.get('station') or layer.get('name') or selection.get('name') or 'Nothing chosen'
            detail = source_chip(active)
        elif name == 'atmosphere':
            source = layer.get('name') or layer.get('title') or selection.get('name') or 'None'
            detail = 'Offline sound' if (layer.get('kind') or selection.get('kind')) == 'local' else 'Stream'
        else:
            source = layer.get('name') or 'Scene'
            detail = ''
        state = 'couldn’t start' if failed else 'closed' if closed and name == 'picture' else (
            'stopped' if closed else status.lower())
        row = {'layer': name, 'title': LAYER_WORDS[name], 'source': source, 'detail': detail,
               'state': state, 'failed': failed, 'closed': closed, 'error': errors.get(name)}
        if name in ('music', 'atmosphere'):
            level = levels.get(name)
            row.update(level=level if isinstance(level, (int, float)) else selection.get('volume', 50),
                       muted=bool(layer.get('muted', selection.get('muted'))),
                       can_next=(can_skip(snapshot) if name == 'music' else bool(layer.get('can_next'))))
        if name == 'atmosphere':
            row.update(outdoor=bool(layer.get('outdoor') or selection.get('outdoor')),
                       window=layer.get('window_open', selection.get('window_open', 35)))
        if name == 'picture':
            row.update(placement=layer.get('placement') or selection.get('placement') or '9',
                       moving=not (selection.get('still') or layer.get('still')))
        rows.append(row)
    return rows


def scene_assets(active, presets):
    """Still art and reviewed loop for the active room's scene, if it has them."""
    preset_id = ((active or {}).get('picture') or {}).get('preset_id')
    preset = next((item for item in presets if item.get('id') == preset_id), {})
    return {'still': preset.get('still_url') or (active or {}).get('poster_path') or preset.get('poster_path'),
            'poster': (active or {}).get('poster_path') or preset.get('poster_path'),
            'loop': preset.get('url') if preset.get('kind') == 'local' else None}


def room_title(snapshot):
    """What an ad-hoc room is called: the station or source that plays, then the controller's name."""
    active = (snapshot or {}).get('active') or {}
    music = active.get('music') or {}
    station = music.get('station') if isinstance(music.get('station'), dict) else {}
    layer = _layer(snapshot or {}, 'music')
    for name in (station.get('name'), layer.get('station'), music.get('name'), layer.get('name'), active.get('name')):
        if not generic_name(name):
            return name
    return 'Your listening room'


GENERIC_NAMES = frozenset({'', 'your listening room', 'listening room', 'untitled'})


def generic_name(name):
    """True for a name that says nothing about the room ("Your listening room", empty)."""
    return (name or '').strip().lower() in GENERIC_NAMES


def shown_name(snapshot, catalogue=()):
    """The active room's title: its own name, or what plays when it is ad hoc or has only a generic name."""
    active = (snapshot or {}).get('active') or {}
    if ad_hoc(snapshot, catalogue) or generic_name(active.get('name')):
        return room_title(snapshot)
    return active.get('name')


def suggested_name(snapshot):
    """What Save as my room offers: the room's own name, else what plays."""
    name = ((snapshot or {}).get('active') or {}).get('name')
    return room_title(snapshot) if generic_name(name) else name


def fallback_scene(snapshot, presets, catalogue=()):
    """Art for a room with no scene of its own: the last room's scene, else the first room that has art."""
    last = (snapshot or {}).get('last_session') or {}
    assets = scene_assets(last, presets) if last else {}
    if assets.get('still') or assets.get('loop'):
        return dict(assets, loop=None)
    for room in catalogue or ():
        preset = next((item for item in presets if item.get('id') == room.get('view_id')), {})
        still = preset.get('still_url') or room.get('poster_path')
        if still:
            return {'still': still, 'poster': room.get('poster_path') or still, 'loop': None}
    return {'still': None, 'poster': None, 'loop': None}


def motion_allowed(snapshot, animations_enabled=True):
    settings = snapshot.get('settings') or {}
    picture = (snapshot.get('active') or {}).get('picture') or {}
    return bool(animations_enabled and not settings.get('reduced_motion')
                and not settings.get('still') and not picture.get('still'))


_TECHNICAL = re.compile(r'\[Errno|Errno \d|Traceback|Exception|[A-Z][a-z]+Error\b|\bErr\w*:|0x[0-9a-f]{4,}|'
                        r'(^|\s)/\S+|\b(?:file|line) \d+|[{}<>]')


def plain_error(error):
    """A controller or socket error in words a listener can act on; never the raw cause."""
    text = str(error or '').strip()
    low = text.lower()
    if not text:
        return 'Something went wrong. Try again.'
    if 'errno' in low or 'refused' in low or 'no such file' in low or 'broken pipe' in low or 'unreachable' in low:
        return 'The room service did not answer. Try again in a moment.'
    if 'timed out' in low or 'timeout' in low or 'took too long' in low:
        return 'It took too long to connect.'
    if _TECHNICAL.search(text) or len(text) > 160:
        return 'Something went wrong. Try again, or open Details to see why.'
    return text


def error_details(error):
    """The raw cause, for a Details expander, or '' when the plain sentence already says it all."""
    text = str(error or '').strip()
    return text if text and text != plain_error(text) else ''


def hyprland(environ=None):
    """True inside a Hyprland session: scene placement (workspace, on top, small) needs its hyprctl."""
    import os
    return bool((os.environ if environ is None else environ).get('HYPRLAND_INSTANCE_SIGNATURE'))


def _music_identity(music):
    music = music or {}
    station = music.get('station') if isinstance(music.get('station'), dict) else None
    if station:
        return ('station', station.get('id'))
    reference = music.get('reference') if isinstance(music.get('reference'), dict) else None
    if reference:
        return ('reference', reference.get('provider'), reference.get('id'))
    return ('source', music.get('source_id') or music.get('url'))


def room_base(snapshot, catalogue=()):
    """The saved room or catalogue room the active room was entered from, or None for an ad-hoc room."""
    active = (snapshot or {}).get('active') or {}
    variant = next((item for item in (snapshot or {}).get('variants') or []
                    if isinstance(item, dict) and item.get('id') == active.get('id')), None)
    if variant is not None:
        return {'name': variant.get('name'), 'music': variant.get('music') or {}}
    room = next((item for item in catalogue or () if item.get('id') == active.get('room_id')), None)
    if room is not None and room.get('source_id'):
        return {'name': room.get('name'), 'music': {'source_id': room['source_id']}}
    return None


def room_changed(snapshot, catalogue=()):
    """True when Tune in (or Change…) replaced the music of a saved or catalogue room."""
    active = (snapshot or {}).get('active')
    base = room_base(snapshot, catalogue) if active else None
    return bool(base and _music_identity(active.get('music')) != _music_identity(base['music']))


def ad_hoc(snapshot, catalogue=()):
    """An active room started from Listen with no room behind it (no saved room, no catalogue room)."""
    active = (snapshot or {}).get('active')
    return bool(active) and room_base(snapshot, catalogue) is None and not active.get('room_id')
