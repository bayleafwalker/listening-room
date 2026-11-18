"""Station and history vocabulary for Listen, derived from controller snapshots.

Plain data in, plain data out, so the mood mapping onto the existing station
preference values, the descriptors, the history rows and the confirmation
wording can be tested without GTK.
"""
import datetime
import re

# Each mood choice maps onto one value of station_engine.PREFERENCE_VALUES.
# "Fewer" writes the model-ranked value; a station already set to "Less speech"
# is shown as Fewer and keeps its value until another Voice choice is made.
MOODS = (
    {'key': 'energy', 'title': 'Energy',
     'choices': (('Calm', 'Low'), ('Any', 'Any'), ('Lively', 'High'))},
    {'key': 'familiarity', 'title': 'Familiarity',
     'choices': (('Familiar', 'Familiar'), ('A mix', 'Any'), ('Discover', 'New'))},
    {'key': 'speech', 'title': 'Voices',
     'choices': (('Any', 'Any'), ('Fewer', 'Likely instrumental'), ('Instrumental only', 'Strict instrumental'))},
    {'key': 'stay_close', 'title': 'Range',
     'choices': (('Any', 'Any'), ('Stay close', 'Stay close'), ('Explore', 'Explore'))},
)
ARCS = (('Steady', 'none'), ('Wind down', 'wind_down'), ('Rise', 'rise'))
ENDS = (('Stop', 'stop'), ('Repeat', 'repeat'))
FEWER_VALUES = ('Less speech', 'Likely instrumental')
UNREACHABLE_SPEECH = ('Speech welcome',)
TINTS = 6


def _preferences(station):
    value = (station or {}).get('preferences')
    return value if isinstance(value, dict) else {}


def _mood(key):
    return next(mood for mood in MOODS if mood['key'] == key)


def _label_for(mood, current):
    for label, value in mood['choices']:
        if value == current:
            return label
    if mood['key'] == 'speech' and current in FEWER_VALUES:
        return 'Fewer'
    return 'Any'


def choices(station):
    """The visible choice for each mood, folded onto the three-choice control."""
    preferences = _preferences(station)
    return {mood['key']: _label_for(mood, preferences.get(mood['key'])) for mood in MOODS}


def value_for(key, label):
    """The existing station preference value a choice writes."""
    for name, value in _mood(key)['choices']:
        if name == label:
            return value
    raise ValueError('Unknown ' + str(key) + ' choice: ' + str(label))


def with_choice(station, key, label):
    """A copy of the station with one mood changed; only that key is rewritten."""
    result = dict(station or {})
    result['preferences'] = dict(_preferences(station), **{key: value_for(key, label)})
    return result


def undo_of(station, key):
    """A function that puts back only this one setting, whatever else changed since.

    `key` is a mood key, 'arc' or 'end'. The returned function takes the current
    station and returns a copy with just that setting restored to what it was
    in `station`.
    """
    if key in ('arc', 'end', 'name'):
        field = {'arc': 'arc', 'end': 'end_policy', 'name': 'name'}[key]
        present, value = field in (station or {}), (station or {}).get(field)

        def restore(current):
            result = dict(current or {})
            if present:
                result[field] = value
            else:
                result.pop(field, None)
            return result
        return restore
    preferences = _preferences(station)
    present, value = key in preferences, preferences.get(key)

    def restore_preference(current):
        result = dict(current or {})
        updated = dict(_preferences(current))
        if present:
            updated[key] = value
        else:
            updated.pop(key, None)
        result['preferences'] = updated
        return result
    return restore_preference


def reinserted(station, entry, index):
    """The station with one removed fallback back at its old position (clamped), unless already there."""
    items = list((station or {}).get('fallbacks') or [])
    if entry in items:
        return dict(station or {})
    items.insert(max(0, min(index, len(items))), entry)
    return with_fallbacks(station, items)


def arc(station):
    value = (station or {}).get('arc')
    return value if value in ('wind_down', 'rise') else 'none'


def arc_label(station):
    return next((label for label, value in ARCS if value == arc(station)), 'Steady')


def with_arc(station, label):
    value = next((value for name, value in ARCS if name == label), 'none')
    return dict(station or {}, arc=value)


def end_label(station):
    return 'Repeat' if (station or {}).get('end_policy') == 'repeat' else 'Stop'


def with_end(station, label):
    return dict(station or {}, end_policy='repeat' if label == 'Repeat' else 'stop')


def first_source(station):
    sources = (station or {}).get('sources') or []
    first = sources[0] if sources and isinstance(sources[0], dict) else {}
    return first


def kind_word(station):
    return 'Library' if (first_source(station).get('reference') or {}) else 'Radio'


def programmable(station):
    """Mood programming, a hold switch and upcoming tracks need a library station."""
    return bool(first_source(station).get('reference'))


def name(station):
    value = (station or {}).get('name')
    return str(value) if isinstance(value, str) and value.strip() else 'Station'


def origin(station):
    """Where a station draws its music, in one line."""
    first = first_source(station)
    reference = first.get('reference') or {}
    if reference:
        parts = ['Library station']
        genres = reference.get('genres') or []
        if genres and isinstance(genres[0], str):
            parts.append(genres[0])
        parts.append('Jellyfin')
        return ' · '.join(parts)
    label = first.get('name') or first.get('group') or 'Radio'
    return 'Radio station · ' + str(label)


def descriptor(station):
    """One line for a row or card: Library · winds down, Radio · 2 fallbacks."""
    parts = [kind_word(station)]
    if arc(station) == 'wind_down':
        parts.append('winds down')
    elif arc(station) == 'rise':
        parts.append('rises')
    if _preferences(station).get('speech') == 'Strict instrumental':
        parts.append('instrumental only')
    count = len((station or {}).get('fallbacks') or [])
    if count:
        parts.append('%d fallback%s' % (count, '' if count == 1 else 's'))
    if len(parts) == 1:
        first = first_source(station)
        if first.get('name'):
            parts.append(str(first['name']))
        elif first.get('group'):
            parts.append(str(first['group']))
    return ' · '.join(parts[:3])


def fallback_source_name(item):
    item = item if isinstance(item, dict) else {}
    reference = item.get('reference') or {}
    value = item.get('name') or reference.get('name') or item.get('source_id') or item.get('id') or 'Source'
    return str(value)


def fallbacks(station):
    """The ordered fallbacks with an index, a name and where each one comes from."""
    result = []
    for index, item in enumerate((station or {}).get('fallbacks') or []):
        if not isinstance(item, dict):
            continue
        result.append({'index': index, 'name': fallback_source_name(item),
                       'detail': 'Jellyfin' if item.get('reference') else ('Radio' if item.get('url') else 'Saved source')})
    return result


def moved(items, source, target):
    """Move one list item to another index; out-of-range moves are ignored."""
    values = list(items or [])
    if not 0 <= source < len(values) or not 0 <= target < len(values) or source == target:
        return values
    value = values.pop(source)
    values.insert(target, value)
    return values


def with_fallbacks(station, items):
    return dict(station or {}, fallbacks=[dict(item) for item in items or [] if isinstance(item, dict)])


def hints(station, reviewed_count=None, more=False):
    """The one hint for the selected constrained Voice value, or None.

    ``reviewed_count`` is the number of human-reviewed instrumental recordings
    the whole library holds; None means it is not known yet. ``more`` says the
    count stopped early, so it is shown as a lower bound.
    """
    speech = _preferences(station).get('speech')
    if speech == 'Strict instrumental':
        if reviewed_count is None:
            return 'Plays only recordings you have reviewed.'
        if reviewed_count == 0:
            return 'No reviewed recordings yet. Review them in Preferences › Recordings.'
        return ('%d%s reviewed recording%s available across your whole library. Review more in Preferences › Recordings.'
                % (reviewed_count, '+' if more else '', '' if reviewed_count == 1 and not more else 's'))
    if speech == 'Likely instrumental':
        return 'Fewer uses model estimates and can be wrong.'
    if speech == 'Less speech':
        return 'Fewer skips tracks tagged spoken word.'
    return None


def range_hint(station):
    """What Range does, for the selected value; None for Any."""
    return {'Stay close': 'Favours the genres of the track that just played.',
            'Explore': 'Steers away from the genres of the track that just played.'}.get(_preferences(station).get('stay_close'))


def when_words(stamp, now):
    """When something was played: Just now, Today, Monday evening, A while ago."""
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not isinstance(now, (int, float)):
        return 'Earlier'
    if stamp > now or now - stamp < 3600:
        return 'Just now'
    try:
        moment = datetime.datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return 'Earlier'
    days = int((now - stamp) // 86400)
    if days <= 0:
        return 'Today'
    if days == 1:
        return 'Yesterday'
    if days < 7:
        part = 'morning' if 5 <= moment.hour < 12 else 'afternoon' if 12 <= moment.hour < 17 else 'evening'
        return moment.strftime('%A') + ' ' + part
    if days < 30:
        return 'A few weeks ago'
    return 'A while ago'


def history_rows(entries, now):
    """Recently played rows: Replay only when the entry kept track references."""
    rows = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        station = entry.get('station') if isinstance(entry.get('station'), dict) else {}
        tracks = entry.get('tracks') if isinstance(entry.get('tracks'), list) else []
        count = len([item for item in tracks if isinstance(item, dict)])
        parts = ['Station', ('%d track%s' % (count, '' if count == 1 else 's')) if count else 'no tracks kept',
                 when_words(entry.get('time'), now)]
        rows.append({'id': entry.get('id'), 'name': name(station), 'tracks': count,
                     'station_id': station.get('id'), 'station': station,
                     'action': 'replay' if count else 'tune', 'detail': ' · '.join(parts)})
    return rows


def _program_for(program, station):
    if not isinstance(program, dict):
        return None
    # A radio station reports its definition without a program behind it: it has
    # no explanation, no hold and nothing upcoming.
    if 'explanation' not in program:
        return None
    if (program.get('definition') or {}).get('id') != (station or {}).get('id'):
        return None
    return program


def learned(program, station):
    """What the running program says it used; empty when this station is not running."""
    matched = _program_for(program, station)
    if matched is None:
        return []
    return [str(line) for line in (matched.get('explanation') or []) if isinstance(line, str)]


def upcoming(program, station, limit=8):
    matched = _program_for(program, station)
    if matched is None:
        return []
    rows = []
    for item in matched.get('upcoming') or []:
        if not isinstance(item, dict):
            continue
        rows.append({'name': str(item.get('name') or 'Track'),
                     'detail': str(item.get('artist') or item.get('album') or '')})
    return rows[:limit]


def hold(program, station):
    """True or False when this station is running, None when the switch cannot work."""
    matched = _program_for(program, station)
    return None if matched is None else bool(matched.get('hold'))


def toast_text(key, label):
    """One confirmation per autosaved choice."""
    if key == 'energy':
        return 'Energy set to ' + str(label)
    if key == 'familiarity':
        return 'Familiarity set to ' + str(label)
    if key == 'speech':
        return 'Voices: ' + str(label)
    if key == 'stay_close':
        return 'Range: ' + str(label)
    if key == 'name':
        return 'Renamed to ' + str(label)
    if key == 'arc':
        return {'Wind down': 'Wind down is on for the next track',
                'Rise': 'Rise is on for the next track'}.get(label, 'Mood stays steady')
    if key == 'end':
        return 'Repeat when it runs out' if label == 'Repeat' else 'Stop when it runs out'
    if key == 'hold':
        return 'Holding this mood' if label else 'Mood can change again'
    return 'Station updated'


def initials(value, two=True):
    words = [word for word in re.split(r'[^0-9A-Za-z]+', str(value or '')) if word]
    if not words:
        return '·'
    if len(words) == 1 or not two:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def tint_index(value, count=TINTS):
    return sum(ord(character) for character in str(value or '')) % max(1, count)


def station_from_source(item):
    """A new station that plays exactly the chosen source."""
    item = item if isinstance(item, dict) else {}
    reference = item.get('reference')
    if reference or item.get('provider') == 'jellyfin':
        reference = reference or item
        source = {'reference': {key: reference[key] for key in
                                ('provider', 'id', 'name', 'type', 'artist', 'album', 'genres', 'tags',
                                 'image_id', 'duration', 'favorite', 'play_count')
                                if reference.get(key) is not None}}
    else:
        source = {'source_id': item.get('id'), 'id': item.get('id'), 'name': item.get('name'),
                  'url': item.get('url'), 'kind': 'radio'}
        if item.get('resolver'):
            source['resolver'] = True
    return {'name': item.get('name') or 'Station', 'sources': [source], 'fallbacks': [],
            'preferences': {}, 'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'}}


# ---- a station made from scratch ----------------------------------------------------

LINK_PROBLEM = ('That link cannot be a station. Paste a direct stream (such as .mp3 or .m3u8), '
                'a YouTube video or a Twitch channel.')


def source_from_link(preview):
    """The station source a validated link stands for, or None when it cannot play as music.

    ``preview`` is what next_links.descriptor said about the link; only links that offer
    "Play" qualify, so the rules are exactly the ones the link sheet already applies.
    """
    preview = preview if isinstance(preview, dict) else {}
    if 'music' not in (preview.get('actions') or []) or not preview.get('url'):
        return None
    kind = preview.get('kind')
    if kind == 'youtube':
        return {'source_id': 'youtube-video', 'id': 'youtube-video', 'name': preview.get('name') or 'YouTube audio',
                'url': preview['url'], 'kind': 'radio', 'resolver': True}
    if kind == 'twitch':
        return {'source_id': 'twitch-channel', 'id': 'twitch-channel', 'name': preview.get('name') or 'Twitch channel',
                'url': preview['url'], 'kind': 'radio', 'resolver': True}
    return {'source_id': 'custom-stream', 'id': 'custom-stream', 'name': preview.get('name') or 'Your stream',
            'url': preview['url'], 'kind': 'radio'}


def source_from_video(item):
    """A YouTube search row as a station source (played through the resolver, like a pasted link)."""
    return {'source_id': 'youtube-video', 'id': 'youtube-video', 'name': item.get('name') or 'YouTube audio',
            'url': item.get('url'), 'kind': 'radio', 'resolver': True}


def source_from_channel(item):
    login = item.get('login') or item.get('name') or 'channel'
    return {'source_id': 'twitch-channel', 'id': 'twitch-channel', 'name': item.get('name') or login,
            'url': item.get('url') or 'https://www.twitch.tv/' + str(login), 'kind': 'radio', 'resolver': True}


def is_library_source(source):
    return bool((source or {}).get('reference'))


def new_station(name, source, preferences=None, similar=False):
    """A station with a name, one seed source and the chosen moods; nothing else is assumed."""
    item = dict(source or {})
    station = station_from_source(item)
    station['name'] = ' '.join(str(name or '').split()) or (item.get('name') or 'Station')
    allowed = {mood['key']: {value for _label, value in mood['choices']} for mood in MOODS}
    station['preferences'] = {key: value for key, value in (preferences or {}).items()
                              if value in allowed.get(key, ())}
    if similar and is_library_source(station['sources'][0]):
        station['generation'] = {'kind': 'audiomuse'}
    return station
