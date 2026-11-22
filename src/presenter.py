"""The optional, best-effort station presenter.

Preparation is deliberately separate from playback.  A failed or slow provider only
records a dropped slot: music carries on and no audio is retained in station state.
"""
import json
import os
from pathlib import Path
import tempfile
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
                    'segments': [{k: v for k, v in segment.items() if not k.startswith('_')}
                                 for segment in self.segments], 'level': self.level, 'muted': self.muted}

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
            if clip.get('placement') != 'between_tracks' or not clip.get('source'):
                self.record_drop('invalid clip', sources); return None
            text = clip.get('text', '')
        elif not sources:
            self.record_drop('no captured metadata'); return None
        else:
            try:
                text = self._chat(sources, setting['budget']['max_seconds'], timeout)
            except (ValueError, OSError, TimeoutError, urllib.error.URLError) as error:
                with self._lock: self.status = str(error) or 'Presenter unavailable; music only'
                self.record_drop('language unavailable', sources); return None
        if not isinstance(text, str) or not text.strip() or len(text) > setting['budget']['max_seconds'] * 18:
            self.record_drop('malformed generation', sources); return None
        text = text.strip()
        with self._lock:
            if text in self._texts:
                self.record_drop('repeated segment', sources); return None
        try:
            audio = clip.get('audio') if clip else self._speech(text, timeout)
            path = self._runtime_audio(audio)
        except (ValueError, OSError, TypeError) as error:
            with self._lock: self.status = str(error) or 'Presenter voice unavailable; music only'
            self.record_drop('voice unavailable', sources); return None
        segment = {'text': text, 'sources': sources, 'outcome': 'played', '_audio_path': str(path),
                   '_max_seconds': setting['budget']['max_seconds']}
        with self._lock:
            self.status = ''
            self.segments.insert(0, segment); self._texts.insert(0, text)
            del self.segments[self.history:]; del self._texts[self.history:]
        return segment

    def discard(self, segment):
        """Remove the ephemeral voice file once its one scheduled slot is over."""
        if isinstance(segment, dict):
            try: Path(segment.get('_audio_path', '')).unlink(missing_ok=True)
            except (OSError, TypeError): pass

    def _runtime_audio(self, audio):
        if not isinstance(audio, bytes) or not audio:
            raise ValueError('Presenter voice unavailable; music only')
        root = os.environ.get('XDG_RUNTIME_DIR')
        if not root:
            raise ValueError('Presenter voice unavailable; music only')
        directory = Path(root) / 'listening-hub' / 'presenter'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='segment-', suffix='.audio', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(audio)
            return Path(name)
        except Exception:
            Path(name).unlink(missing_ok=True)
            raise

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
        except (online_services.ConnectionError, urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
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
        return raw
