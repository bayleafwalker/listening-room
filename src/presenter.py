"""The optional, best-effort station presenter.

Preparation is deliberately separate from playback.  A failed or slow provider only
records a dropped slot: music carries on and no audio is retained in station state.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import online_services

DEFAULT_BUDGET = {'every_tracks': 4, 'max_seconds': 18}


def budget(value=None):
    value = value or {}
    every = value.get('every_tracks', DEFAULT_BUDGET['every_tracks'])
    length = value.get('max_seconds', DEFAULT_BUDGET['max_seconds'])
    if isinstance(every, bool) or not isinstance(every, int) or not 2 <= every <= 20:
        raise ValueError('Presenter frequency must be from 2 to 20 tracks')
    if isinstance(length, bool) or not isinstance(length, int) or not 5 <= length <= 90:
        raise ValueError('Presenter length must be from 5 to 90 seconds')
    return {'every_tracks': every, 'max_seconds': length}


def preferences(value=None):
    value = value or {}
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('Presenter must be on or off')
    return {'enabled': enabled, 'budget': budget(value.get('budget'))}


def metadata_sources(item):
    """Only captured track fields become facts, and are named in the receipt."""
    if not isinstance(item, dict):
        return []
    result = []
    for key in ('name', 'artist', 'album'):
        if isinstance(item.get(key), str) and item[key].strip():
            result.append({'field': key, 'value': item[key].strip(), 'source': 'captured metadata'})
    return result


class Presenter:
    def __init__(self, clock=time.time, history=12):
        self.clock, self.history = clock, history
        self.segments, self.status, self._texts = [], '', []
        self.level, self.muted = 50., False
        self._lock = threading.RLock()

    def snapshot(self, station=None):
        setting = preferences((station or {}).get('presenter'))
        with self._lock:
            return {'enabled': setting['enabled'], 'budget': setting['budget'], 'status': self.status,
                    'segments': list(self.segments), 'level': self.level, 'muted': self.muted}

    def record_drop(self, reason, sources=()):
        with self._lock:
            self.segments.insert(0, {'text': '', 'sources': list(sources), 'outcome': 'dropped', 'reason': reason})
            del self.segments[self.history:]

    def prepare(self, station, item, primary_speech=False, clip=None, timeout=3):
        setting = preferences((station or {}).get('presenter'))
        sources = metadata_sources(item)
        if not setting['enabled']:
            return None
        if primary_speech:
            self.record_drop('primary speech source', sources); return None
        if clip:
            if clip.get('expires_at', float('inf')) < self.clock():
                self.record_drop('clip expired', sources); return None
            text = clip.get('text', '')
        elif not sources:
            self.record_drop('no captured metadata'); return None
        else:
            text = self._chat(sources, setting['budget']['max_seconds'], timeout)
        if not isinstance(text, str) or not text.strip() or len(text) > setting['budget']['max_seconds'] * 18:
            self.record_drop('malformed generation', sources); return None
        text = text.strip()
        with self._lock:
            if text in self._texts:
                self.record_drop('repeated segment', sources); return None
        if not clip:
            try:
                self._speech(text, timeout)
            except ValueError as error:
                with self._lock: self.status = str(error)
                self.record_drop('voice unavailable', sources); return None
        segment = {'text': text, 'sources': sources, 'outcome': 'played'}
        with self._lock:
            self.status = ''
            self.segments.insert(0, segment); self._texts.insert(0, text)
            del self.segments[self.history:]; del self._texts[self.history:]
        return segment

    def _request(self, service, path, payload, timeout):
        try:
            context = online_services._request_context(service, required=False)
            endpoint = context['settings']['endpoint'].rstrip('/') + path
            headers = {'Content-Type': 'application/json'}
            if context['token']: headers['Authorization'] = 'Bearer ' + context['token']
            request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200: raise ValueError('presenter service unavailable')
                return response.read(2 * 1024 * 1024)
        except (online_services.ConnectionError, urllib.error.URLError, TimeoutError, OSError) as error:
            raise ValueError('Presenter unavailable; music only') from error

    def _chat(self, sources, seconds, timeout):
        prompt = 'Write one brief presenter link using only this captured metadata: ' + json.dumps(sources)
        raw = self._request('presenter_llm', '/v1/chat/completions',
                            {'model': 'presenter', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': seconds * 3}, timeout)
        try: return json.loads(raw)['choices'][0]['message']['content']
        except (ValueError, KeyError, IndexError, TypeError): return ''

    def _speech(self, text, timeout):
        raw = self._request('presenter_voice', '/v1/audio/speech', {'model': 'presenter', 'voice': 'default', 'input': text}, timeout)
        if not raw: raise ValueError('Presenter voice unavailable; music only')
