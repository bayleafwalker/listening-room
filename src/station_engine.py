"""Saved station intent and bounded, provider-neutral programming state.

Only user-approved sources are used. Provider resolution is supplied by the
caller; prepared URLs and credentials never belong in this durable model.
"""
import copy
import hashlib
import math
import random
import re
import urllib.parse

import listening_hub as hub

MAX_QUEUE = 8
MAX_RECENT = 40
PREFERENCES = ('energy', 'familiarity', 'speech', 'stay_close')
PREFERENCE_VALUES = {'energy': ('Any', 'Low', 'High'), 'familiarity': ('Any', 'Familiar', 'New'),
                     'speech': ('Any', 'Less speech', 'Speech welcome', 'Likely instrumental', 'Strict instrumental'),
                     'stay_close': ('Any', 'Stay close', 'Explore')}


def _source(value):
    if not isinstance(value, dict):
        raise ValueError('Choose a supported station source')
    result = {k: copy.deepcopy(value[k]) for k in ('source_id', 'id', 'name', 'url', 'resolver', 'reference', 'kind') if k in value}
    reference = result.get('reference')
    if reference:
        if not isinstance(reference, dict) or reference.get('provider') != 'jellyfin' or not reference.get('id'):
            raise ValueError('Choose a Jellyfin library item or direct source')
        if (not isinstance(reference.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', reference['id'])
                or reference.get('type') not in ('Audio', 'Playlist', 'MusicAlbum')):
            raise ValueError('Choose a valid Jellyfin library item')
        result['reference'] = {'provider': 'jellyfin', 'id': reference['id'], 'type': reference['type']}
        for key in ('name', 'artist', 'album'):
            if isinstance(reference.get(key), str):
                result['reference'][key] = hub.clean(reference[key])[:300]
        result.pop('url', None)
    elif result.get('url'):
        if not hub.valid_url(result['url']):
            raise ValueError('Choose a valid public stream URL')
        query = urllib.parse.urlsplit(result['url']).query
        if (hub.private_favorite(result)
                or any(term in key.casefold() for key, _ in urllib.parse.parse_qsl(query) for term in ('token', 'auth', 'key', 'secret', 'signature', 'password', 'credential'))):
            raise ValueError('Save private streams as a favorite before adding them to a station')
    elif not (result.get('source_id') or result.get('id')):
        raise ValueError('Choose a supported station source')
    return result


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('Station must be an object')
    name = hub.clean(value.get('name'))
    if not name:
        raise ValueError('Name your station')
    sources = value.get('sources')
    fallbacks = value.get('fallbacks', [])
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20 or not isinstance(fallbacks, list) or len(fallbacks) > 20:
        raise ValueError('Choose one or more station sources')
    prefs = value.get('preferences') or {}
    if not isinstance(prefs, dict):
        raise ValueError('Station preferences must be an object')
    clean_prefs = {}
    for key in PREFERENCES:
        if key in prefs:
            val = prefs[key]
            if not isinstance(val, str) or val not in PREFERENCE_VALUES[key]:
                raise ValueError('Invalid station preference')
            clean_prefs[key] = val
    policy = value.get('end_policy', 'stop')
    if policy not in ('stop', 'repeat'):
        raise ValueError('Choose stop or repeat for the station end')
    arc = value.get('arc', 'none')
    if arc not in ('none', 'wind_down', 'rise'):
        raise ValueError('Choose a supported program arc')
    identity = value.get('id') or 'station-' + hashlib.sha256(name.encode()).hexdigest()[:16]
    if not isinstance(identity, str) or len(identity) > 100 or not identity:
        raise ValueError('Invalid station identity')
    generation = value.get('generation') or {'kind': 'collection'}
    if not isinstance(generation, dict) or generation.get('kind') not in ('collection', 'audiomuse'):
        raise ValueError('Choose a supported station generator')
    return {'id': identity, 'name': name, 'sources': [_source(s) for s in sources],
            'fallbacks': [_source(s) for s in fallbacks], 'preferences': clean_prefs,
            'end_policy': policy, 'arc': arc, 'generation': {'kind': generation['kind']}}


def identity(item):
    return (item.get('provider'), item.get('id'), item.get('type'))


def instrumental_score(metadata):
    if not metadata or not metadata.get('audio_sha256'):
        return None
    for feature in reversed(metadata.get('features', [])):
        value = feature.get('value')
        if (feature.get('name') == 'instrumental score'
                and feature.get('provider') == 'Essentia MTG'
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 <= value <= 1):
            return value
    return None


class Program:
    """In-memory queue; refills select only from the chosen collection."""
    def __init__(self, station, candidates, seed=None, trusted_resolver=None):
        self.station = normalize(station)
        self.trusted_resolver = trusted_resolver
        self.candidates = [copy.deepcopy(x) for x in candidates if isinstance(x, dict)]
        self.random = random.Random(seed)
        self.queue = []
        self.override = None
        self.recent = []
        self.played = []
        self.consumed = set()
        self.liked_artists = set()
        self.disliked = set()
        self.hold = False
        self.replay = None
        self.refill()

    def _metadata(self, item):
        if self.trusted_resolver is None:
            return None
        try:
            return self.trusted_resolver(item)
        except (ValueError, OSError, RuntimeError):
            return None

    def _eligible(self, item):
        if self.station['preferences'].get('speech') != 'Strict instrumental':
            return True
        metadata = self._metadata(item) or {}
        return (metadata.get('review') or {}).get('classification') == 'instrumental'

    def __deepcopy__(self, memo):
        result = object.__new__(type(self))
        memo[id(self)] = result
        for key, value in self.__dict__.items():
            setattr(result, key, value if key == 'trusted_resolver' else copy.deepcopy(value, memo))
        return result

    def refill(self):
        if self.replay is not None:
            count = MAX_QUEUE - len(self.queue)
            while self.replay and count:
                item = self.replay.pop(0)
                if self._eligible(item):
                    self.queue.append(item)
                    count -= 1
            return
        used = self.consumed | {identity(x) for x in self.queue}
        strict = self.station['preferences'].get('speech') == 'Strict instrumental'
        choices = [x for x in self.candidates if identity(x) not in used and identity(x) not in self.disliked
                   and (not strict or self._eligible(x))]
        self.random.shuffle(choices)
        prefs = self.station['preferences']
        def labels(item):
            return {x.casefold() for field in ('genres', 'tags')
                    for x in (item.get(field) if isinstance(item.get(field), list) else []) if isinstance(x, str)}
        def score(item):
            artist = str(item.get('artist') or '').casefold()
            genres = labels(item)
            current_genres = labels(self.recent[-1]) if self.recent else set()
            played_before = bool(item.get('favorite') is True or (isinstance(item.get('play_count'), int) and item['play_count'] > 0)
                                 or (artist and any(str(x.get('artist') or '').casefold() == artist for x in self.played)))
            value = 0
            if artist and artist in self.liked_artists:
                value += 2
            if prefs.get('familiarity') == 'Familiar' or prefs.get('stay_close') == 'Stay close':
                value += int(played_before)
            elif prefs.get('familiarity') == 'New' or prefs.get('stay_close') == 'Explore':
                value -= int(played_before)
            if prefs.get('stay_close') == 'Stay close':
                value += int(bool(genres & current_genres))
            elif prefs.get('stay_close') == 'Explore':
                value -= int(bool(genres & current_genres))
            metadata = self._metadata(item) or {}
            estimates = [f for f in metadata.get('features', []) if f.get('name') == 'energy'
                         and (f.get('confidence') is None or (isinstance(f.get('confidence'), (int, float)) and f['confidence'] >= 0.8))]
            energy = estimates[-1].get('value') if estimates else None
            if isinstance(energy, (int, float)) and not isinstance(energy, bool):
                value += energy if prefs.get('energy') == 'High' else -energy if prefs.get('energy') == 'Low' else 0
            if prefs.get('speech') == 'Likely instrumental':
                estimate = instrumental_score(metadata)
                if estimate is not None:
                    value += 2 * estimate - 1
            if isinstance(item.get('speech'), bool) and prefs.get('speech') == 'Less speech':
                value -= int(item['speech'])
            elif prefs.get('speech') == 'Less speech' and genres & {'spoken word', 'podcast', 'audiobook'}:
                value -= 1
            return value
        choices.sort(key=score, reverse=True)
        self.queue.extend(copy.deepcopy(choices[:MAX_QUEUE - len(self.queue)]))

    def explain(self, item):
        metadata = self._metadata(item) or {}
        preferences = self.station['preferences']
        reasons = []
        if (metadata.get('review') or {}).get('classification') == 'instrumental':
            reasons.append('Human-reviewed instrumental recording; playback checks the audio again')
        if preferences.get('speech') == 'Likely instrumental' and instrumental_score(metadata) is not None:
            reasons.append('Ranking uses a whole-recording instrumental model score; it is an uncalibrated estimate, not a review')
        energy = [f for f in metadata.get('features', []) if f.get('name') == 'energy']
        if preferences.get('energy') in ('Low', 'High') and energy:
            reasons.append('Energy preference uses a raw audio measurement; subjective energy and confidence are unknown')
        if preferences.get('familiarity') in ('Familiar', 'New'):
            reasons.append('Familiarity uses favorites and listening history')
        if not reasons:
            reasons.append('Selected from the chosen library collection')
        return reasons

    def retune(self):
        """Reconsider future items, without interrupting the current track."""
        if self.replay is None:
            self.queue = []
            self.refill()

    def feedback(self, rating):
        if not self.recent or rating not in ('more', 'less'):
            raise ValueError('Choose more or less for the current track')
        current = self.recent[-1]
        if rating == 'less':
            self.disliked.add(identity(current))
        elif current.get('artist'):
            self.liked_artists.add(str(current['artist']).casefold())
        self.retune()

    def next(self):
        if self.override is not None:
            item, self.override = self.override, None
            if not self._eligible(item):
                return self.next()
            self.consumed.add(identity(item))
            self.recent = (self.recent + [copy.deepcopy(item)])[-MAX_RECENT:]
            self.played = (self.played + [copy.deepcopy(item)])[-MAX_RECENT:]
            return item
        if not self.queue and self.station['end_policy'] == 'repeat':
            self.recent = []
            self.consumed = set()
            self.refill()
        if not self.queue:
            return None
        item = self.queue.pop(0)
        if not self._eligible(item):
            self.consumed.add(identity(item))
            return self.next()
        self.consumed.add(identity(item))
        self.recent = (self.recent + [copy.deepcopy(item)])[-MAX_RECENT:]
        self.played = (self.played + [copy.deepcopy(item)])[-MAX_RECENT:]
        if not self.hold and len(self.played) % 3 == 0:
            arc = self.station.get('arc')
            if arc in ('wind_down', 'rise'):
                self.station['preferences']['energy'] = 'Low' if arc == 'wind_down' else 'High'
                self.retune()
        self.refill()
        return item

    def play_next(self, item):
        source = _source({'reference': item})
        ref = source['reference']
        if not self._eligible(ref):
            raise ValueError('Strict instrumental requires reviewed instrumental tracks')
        self.queue = [x for x in self.queue if identity(x) != identity(ref)]
        self.override = ref

    def can_next(self):
        if any(self._eligible(x) for x in ([self.override] if self.override is not None else []) + self.queue + (self.replay or [])):
            return True
        if self.replay is not None or self.station['end_policy'] != 'repeat':
            return False
        strict = self.station['preferences'].get('speech') == 'Strict instrumental'
        return any(identity(x) not in self.disliked and
                   (not strict or self._eligible(x))
                   for x in self.candidates)
