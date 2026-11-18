"""Bounded, opt-in recording observations from configured library services.

Existing AudioMuse rows have no analysis timestamp or model revision; timestamps
below indicate retrieval only, and neither these scores nor external IDs are a
human review of the exact recording.
"""
import datetime
import contextlib
import metadata_http
import hashlib
import importlib.util
import os
import json
import math
import re
import secrets
import stat
import sys
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid

import online_services as online

MAX_AUDIO = 512 * 1024 * 1024


def library_origin(settings=None):
    settings = settings if settings is not None else online.config()['jellyfin']
    endpoint = online._endpoint(settings['endpoint'])
    user = online._id(settings['user_id'])
    return 'jellyfin:' + hashlib.sha256((endpoint + '\n' + user).encode()).hexdigest()


def _track(item, cancel_event=None, context=None):
    if not isinstance(item, dict) or item.get('provider') != 'jellyfin' or item.get('type') != 'Audio':
        raise online.ConnectionError('Select a Jellyfin audio track')
    identity = online._id(item.get('id'))
    context = context or online._request_context('jellyfin')
    user = online._id(context['settings']['user_id'])
    data = online._request('jellyfin', '/Users/' + user + '/Items/' + identity,
                           params={'Fields': 'ProviderIds,MediaSources,DateLastMediaAdded,DateModified'}, cancel_event=cancel_event, context=context, bounded=True)
    if not isinstance(data, dict) or data.get('Id') != identity or data.get('Type') != 'Audio':
        raise online.ConnectionError('Jellyfin could not verify this audio track')
    return data


def _source(data):
    sources = data.get('MediaSources')
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        raise online.ConnectionError('A single original media source is required')
    source = sources[0]
    sid = online._id(source.get('Id'))
    size = source.get('Size')
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_AUDIO:
        raise online.ConnectionError('Original audio size is unknown or exceeds 512 MiB')
    if not source.get('ETag') and not data.get('DateModified'):
        raise online.ConnectionError('Jellyfin did not provide a stable media revision')
    # These values are revision indicators, never a content hash.
    return {'id': sid, 'size': size, 'etag': str(source.get('ETag') or '')[:200],
            'modified': str(data.get('DateModified') or '')[:100],
            'duration': data.get('RunTimeTicks')}


def collect_jellyfin(item, cancel_event=None):
    context = online._request_context('jellyfin')
    data = _track(item, cancel_event, context)
    identity = online._id(data['Id'])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    identities = []
    ids = data.get('ProviderIds') or {}
    if isinstance(ids, dict):
        for name in ('MusicBrainzTrack', 'MusicBrainzAlbum', 'MusicBrainzAlbumArtist', 'MusicBrainzArtist'):
            try:
                value = str(uuid.UUID(str(ids.get(name, ''))))
            except (ValueError, TypeError, AttributeError):
                continue
            identities.append({'provider': 'Jellyfin', 'provider_id': value, 'name': name,
                               'method': 'reported ProviderIds', 'date': now})
    try:
        revision = _source(data)
    except online.ConnectionError:
        revision = None
    features = []
    audiomuse_context = None
    try:
        audiomuse_context = online._request_context('audiomuse', required=False)
        score = online._request('audiomuse', '/external/get_score', params={'id': identity},
                                cancel_event=cancel_event, required=False, context=audiomuse_context, bounded=True)
    except online.ConnectionError:
        online._cancel(cancel_event)
        score = None
    if isinstance(score, dict) and score.get('item_id') == identity:
        base = {'provider': 'AudioMuse', 'version': 'analysis version unknown', 'date': now}
        for field, method, bound in [('tempo', 'librosa beat track BPM', 1000), ('energy', 'librosa RMS raw', 100)]:
            value = score.get(field)
            if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= bound:
                features.append(dict(base, name=field, value=value, method=method, model='librosa source version unknown'))
        moods = score.get('mood_vector')
        if isinstance(moods, str) and len(moods) <= 10000:
            for entry in moods.split(',')[:30]:
                name, sep, raw = entry.partition(':')
                if not sep or not re.fullmatch(r'[A-Za-z0-9 _.-]{1,80}', name):
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if math.isfinite(value) and 0 <= value <= 1:
                    features.append(dict(base, name='mood ' + name, value=value, method='stored mood estimate', model='MusiCNN model revision unknown'))
    online._check_context(context)
    if audiomuse_context is not None:
        online._check_context(audiomuse_context)
    return {'origin': library_origin(context['settings']), 'item_id': identity, 'revision': revision,
            'identities': identities, 'features': features}


def _audio_snapshot(item, cancel_event=None, chromaprint=False, classify=False, snapshot=False):
    """Read full original bytes and optionally pass a private temporary copy to fpcalc."""
    context = online._request_context('jellyfin')
    before = _track(item, cancel_event, context)
    source = _source(before)
    identity = online._id(before['Id'])
    endpoint = context['settings']['endpoint']
    token = context['token']
    online._check_context(context)
    query = urllib.parse.urlencode({'Static': 'true', 'MediaSourceId': source['id']})
    headers = {'Authorization': 'MediaBrowser Client="Listening Room", Device="Desktop", DeviceId="listening-room", Version="2.0", Token="' + token + '"', 'Accept-Encoding': 'identity'}
    request = urllib.request.Request(endpoint + '/Audio/' + identity + '/stream?' + query, headers=headers)
    digest = hashlib.sha256()
    size = 0
    audio_file = tempfile.TemporaryFile() if chromaprint or classify or snapshot else None
    try:
        header_seen = False
        with contextlib.closing(metadata_http.exchange(request.full_url, headers, cancel_event=cancel_event, timeout=180)) as events:
            for kind, chunk in events:
                if kind == b'H':
                    if header_seen:
                        raise online.ConnectionError('Invalid audio response')
                    header_seen = True
                    info = json.loads(chunk)
                    length = info.get('length')
                    if info.get('status') != 200 or not isinstance(length, str) or not length.isdigit() or int(length) != source['size']:
                        raise online.ConnectionError('Server did not return the complete original audio')
                elif kind == b'D':
                    if not header_seen:
                        raise online.ConnectionError('Invalid audio response')
                    size += len(chunk)
                    if size > source['size'] or size > MAX_AUDIO:
                        raise online.ConnectionError('Original audio exceeded its expected size')
                    digest.update(chunk)
                    if audio_file:
                        audio_file.write(chunk)
            if not header_seen:
                raise online.ConnectionError('Invalid audio response')
        if size != source['size'] or _source(_track(item, cancel_event, context)) != source:
            raise online.ConnectionError('Original audio changed or was incomplete')
        online._check_context(context)
        if not audio_file:
            return {'audio_sha256': digest.hexdigest()}
        if snapshot:
            audio_file.flush()
            audio_file.seek(0)
            result = {'audio_sha256': digest.hexdigest(), 'revision': source,
                      'origin': library_origin(context['settings']), 'file': audio_file}
            audio_file = None
            return result
        if classify:
            scores = _classify_file(audio_file, cancel_event)
            if _source(_track(item, cancel_event, context)) != source:
                raise online.ConnectionError('Original audio changed during analysis')
            online._check_context(context)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            base = {'provider': 'Essentia MTG', 'provider_id': 'dd054410fa4f9fcb0086b4223a9a5c66dd098c5d4cef485c03af0b0500443b73',
                    'method': 'whole audio 16k logmel padded mean patch v1', 'model': 'voice instrumental msd musicnn 1 extractor 49668ffec47e52e94b96f45930bb46a28a1368d4bdfb5c05378fa834aca616e1',
                    'version': '2', 'date': now}
            return {'audio_sha256': digest.hexdigest(), 'revision': source, 'origin': library_origin(context['settings']), 'features': [dict(base, name='instrumental score', value=scores[0]),
                                                                       dict(base, name='voice score', value=scores[1])]}
        # fpcalc accepts a descriptor path; the anonymous temporary file is not
        # exposed to other users and is closed on every exit path.
        audio_file.flush()
        try:
            process = subprocess.Popen(['fpcalc', '-json', '/proc/self/fd/' + str(audio_file.fileno())],
                                       pass_fds=(audio_file.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise online.ConnectionError('Chromaprint fpcalc is unavailable') from None
        deadline = time.monotonic() + 120
        try:
            while True:
                online._cancel(cancel_event)
                try:
                    output, _ = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise online.ConnectionError('Chromaprint timed out')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode or len(output) > 100000:
            raise online.ConnectionError('Chromaprint could not fingerprint this audio')
        try:
            result = json.loads(output)
            duration, fingerprint = result['duration'], result['fingerprint']
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 1 <= duration <= 14400 or not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,20000}', fingerprint):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise online.ConnectionError('Chromaprint returned an invalid fingerprint') from None
        online._check_context(context)
        return {'audio_sha256': digest.hexdigest(), 'fingerprint': fingerprint, 'duration': round(duration)}
    except metadata_http.TransportError as error:
        raise online.ConnectionError(str(error)) from None
    except (OSError, TimeoutError):
        raise online.ConnectionError('Original audio could not be retrieved') from None
    finally:
        if audio_file:
            audio_file.close()


def acquire_verified_snapshot(item, cancel_event=None):
    """Return an open anonymous original file; caller must close it."""
    return _audio_snapshot(item, cancel_event, snapshot=True)


def fingerprint_jellyfin(item, cancel_event=None):
    """Hash the complete original bytes; revision must stay stable."""
    return _audio_snapshot(item, cancel_event)['audio_sha256']


def chromaprint_jellyfin(item, cancel_event=None):
    """Explicit bounded whole-original fetch and Chromaprint generation."""
    return _audio_snapshot(item, cancel_event, chromaprint=True)

# Public lookups are explicit actions, not a consequence of browsing or caching.
import threading
import time

_public_lock = threading.Lock()
_last_public = 0.0


def _public_request(host, path, params, cancel_event=None):
    global _last_public
    if host not in ('musicbrainz.org', 'api.acoustid.org'):
        raise online.ConnectionError('Invalid public metadata service')
    with _public_lock:
        delay = max(0.0, 1.1 - (time.monotonic() - _last_public))
        if cancel_event is not None:
            if cancel_event.wait(delay):
                raise online.ConnectionError('Request cancelled')
        else:
            time.sleep(delay)
        online._cancel(cancel_event)
        encoded = urllib.parse.urlencode(params)
        headers = {'User-Agent': 'ListeningRoom/2.3 (https://github.com/bayleafwalker/listening-room)', 'Accept': 'application/json'}
        if host == 'api.acoustid.org':
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            request = urllib.request.Request('https://' + host + path, data=encoded.encode(), headers=headers)
        else:
            request = urllib.request.Request('https://' + host + path + '?' + encoded, headers=headers)
        _last_public = time.monotonic()
        try:
            chunks = []
            size = 0
            header_seen = False
            with contextlib.closing(metadata_http.exchange(request.full_url, headers, request.data, cancel_event, 30)) as events:
                for kind, block in events:
                    if kind == b'H':
                        if header_seen or json.loads(block).get('status') != 200:
                            raise online.ConnectionError('Public metadata service rejected the request')
                        header_seen = True
                    elif kind == b'D':
                        if not header_seen:
                            raise online.ConnectionError('Invalid public metadata response')
                        size += len(block)
                        if size > online.MAX_RESPONSE:
                            raise online.ConnectionError('Public metadata response was too large')
                        chunks.append(block)
                if not header_seen:
                    raise online.ConnectionError('Invalid public metadata response')
            raw = b''.join(chunks)
            online._cancel(cancel_event)
            if len(raw) > online.MAX_RESPONSE:
                raise online.ConnectionError('Public metadata response was too large')
            return json.loads(raw)
        except online.ConnectionError:
            raise
        except metadata_http.TransportError as error:
            raise online.ConnectionError(str(error)) from None
        except (OSError, ValueError):
            raise online.ConnectionError('Public metadata service could not complete the request') from None


def musicbrainz_lookup(recording_ids, cancel_event=None):
    """Fetch up to 20 explicit recording IDs; returned data are only candidates."""
    if not isinstance(recording_ids, list) or not 1 <= len(recording_ids) <= 20:
        raise online.ConnectionError('Choose between 1 and 20 recordings')
    ids = []
    for value in recording_ids:
        try:
            ids.append(str(uuid.UUID(value)))
        except (ValueError, TypeError, AttributeError):
            raise online.ConnectionError('Invalid MusicBrainz recording ID') from None
    rows = []
    for identity in ids:
        data = _public_request('musicbrainz.org', '/ws/2/recording/' + identity,
                               {'fmt': 'json', 'inc': 'artist-credits'}, cancel_event)
        if isinstance(data, dict) and data.get('id') == identity:
            rows.append({'provider': 'MusicBrainz', 'provider_id': identity,
                         'title': str(data.get('title') or '')[:200], 'status': 'candidate'})
    return rows


def acoustid_lookup(fingerprint, duration, client_key, cancel_event=None):
    """Look up an explicitly supplied Chromaprint using the user's application key."""
    if not isinstance(client_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', client_key):
        raise online.ConnectionError('Configure an AcoustID application key first')
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,20000}', fingerprint):
        raise online.ConnectionError('Invalid Chromaprint fingerprint')
    if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 14400:
        raise online.ConnectionError('Invalid fingerprint duration')
    data = _public_request('api.acoustid.org', '/v2/lookup', {'client': client_key, 'duration': duration,
                           'fingerprint': fingerprint, 'meta': 'recordings', 'format': 'json'}, cancel_event)
    if not isinstance(data, dict) or data.get('status') != 'ok' or not isinstance(data.get('results'), list):
        raise online.ConnectionError('AcoustID returned an invalid lookup response')
    result = []
    for row in data['results'][:20]:
        if not isinstance(row, dict):
            continue
        score = row.get('score')
        if not isinstance(score, (float, int)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
            continue
        for recording in (row.get('recordings') or [])[:20]:
            try:
                identity = str(uuid.UUID(recording['id']))
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            result.append({'provider': 'AcoustID', 'provider_id': identity, 'confidence': score,
                           'status': 'candidate'})
    return result[:20]


def musicbrainz_search(item, cancel_event=None):
    """Explicit title/artist search. All hits remain unverified candidates."""
    context = online._request_context('jellyfin')
    data = _track(item, cancel_event, context)
    title = data.get('Name')
    artists = data.get('Artists') or []
    if not isinstance(title, str) or not title.strip() or not isinstance(artists, list) or not artists or not isinstance(artists[0], str):
        raise online.ConnectionError('Jellyfin track needs a title and artist for matching')
    def quoted(value):
        value = value.strip()[:200]
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    query = 'recording:' + quoted(title) + ' AND artist:' + quoted(artists[0])
    result = _public_request('musicbrainz.org', '/ws/2/recording',
                             {'query': query, 'fmt': 'json', 'limit': 10}, cancel_event)
    rows = result.get('recordings') if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise online.ConnectionError('MusicBrainz returned an invalid search response')
    candidates = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        try:
            identity = str(uuid.UUID(row.get('id', '')))
        except (TypeError, ValueError, AttributeError):
            continue
        candidates.append({'provider': 'MusicBrainz', 'provider_id': identity,
                           'title': str(row.get('title') or '')[:200], 'status': 'candidate'})
    online._check_context(context)
    return candidates


def classify_stored_embedding(item, cancel_event=None):
    """Infer an unverified estimate from an AudioMuse mean-pooled embedding.

    AudioMuse does not record the input hash, analysis time, or embedding model
    revision. This must never be attached to an exact-audio review as evidence.
    The head is applied to the mean embedding, rather than per-patch scores.
    """
    import os
    context = online._request_context('jellyfin')
    data = _track(item, cancel_event, context)
    identity = data['Id']
    path = os.environ.get('LISTENING_VOICE_MODEL', '')
    if not path:
        raise online.ConnectionError('Voice classifier model is not configured')
    try:
        with open(path, 'rb') as handle:
            weights = handle.read(1000000)
        if hashlib.sha256(weights).hexdigest() != 'dd054410fa4f9fcb0086b4223a9a5c66dd098c5d4cef485c03af0b0500443b73':
            raise online.ConnectionError('Voice classifier weights do not match the documented model')
        import numpy as np
        import onnxruntime as ort
    except (OSError, ImportError):
        raise online.ConnectionError('Voice classifier runtime or model is unavailable') from None
    audiomuse_context = online._request_context('audiomuse', required=False)
    result = online._request('audiomuse', '/external/get_embedding', params={'id': identity},
                             cancel_event=cancel_event, required=False, context=audiomuse_context, bounded=True)
    vector = result.get('embedding') if isinstance(result, dict) and result.get('item_id') == identity else None
    if not isinstance(vector, list) or len(vector) != 200 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise online.ConnectionError('AudioMuse did not return a usable embedding')
    online._cancel(cancel_event)
    try:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(weights, sess_options=options, providers=['CPUExecutionProvider'])
        output = session.run(['activations'], {'embeddings': np.asarray([vector], dtype=np.float32)})[0]
        if output.shape != (1, 2) or not np.isfinite(output).all() or (output < 0).any() or (output > 1).any():
            raise ValueError('Invalid model output')
    except Exception:
        raise online.ConnectionError('Voice classifier could not analyze this embedding') from None
    online._cancel(cancel_event)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base = {'provider': 'Essentia MTG', 'provider_id': 'dd054410fa4f9fcb0086b4223a9a5c66dd098c5d4cef485c03af0b0500443b73',
            'method': 'AudioMuse pooled embedding estimate', 'model': 'voice instrumental msd musicnn 1',
            'version': '2', 'date': now}
    online._check_context(context)
    online._check_context(audiomuse_context)
    return {'audio_sha256': None, 'features': [dict(base, name='instrumental score', value=float(output[0, 0])),
                                               dict(base, name='voice score', value=float(output[0, 1]))]}


def _classify_file(audio_file, cancel_event=None):
    """Run CPU inference in a killable process, keeping audio in a private fd."""
    audio_file.flush()
    child_env = os.environ.copy()
    child_env.update(OPENBLAS_NUM_THREADS='2', OMP_NUM_THREADS='2', NUMBA_NUM_THREADS='2')
    try:
        process = subprocess.Popen([sys.executable, __file__, '--classify-fd', str(audio_file.fileno())],
                                   pass_fds=(audio_file.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env)
    except OSError:
        raise online.ConnectionError('Voice classifier runtime is unavailable') from None
    deadline = time.monotonic() + 180
    try:
        while True:
            online._cancel(cancel_event)
            try:
                output, _ = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise online.ConnectionError('Voice classification timed out')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    if process.returncode or len(output) > 4096:
        raise online.ConnectionError('Voice classifier failed or audio exceeds the 20 minute limit')
    try:
        values = json.loads(output)
        if not isinstance(values, list) or len(values) != 2 or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise ValueError()
    except (ValueError, TypeError):
        raise online.ConnectionError('Voice classifier returned invalid scores') from None
    return values


def classify_jellyfin(item, cancel_event=None):
    """Classify the full original, with an actual content SHA256 and revision recheck."""
    for variable in ('LISTENING_EMBED_MODEL', 'LISTENING_VOICE_MODEL'):
        if not os.path.isfile(os.environ.get(variable, '')):
            raise online.ConnectionError('Voice classifier model is not configured')
    if any(importlib.util.find_spec(name) is None for name in ('numpy', 'librosa', 'onnxruntime')):
        raise online.ConnectionError('Voice classifier runtime is unavailable')
    return _audio_snapshot(item, cancel_event, classify=True)


def _classifier_worker(fd):
    import os
    import numpy as np
    import librosa
    import onnxruntime as ort
    def model(env, expected):
        with open(os.environ[env], 'rb') as handle:
            data = handle.read(10000000)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('unexpected model')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        return ort.InferenceSession(data, sess_options=options, providers=['CPUExecutionProvider'])
    extractor = model('LISTENING_EMBED_MODEL', '49668ffec47e52e94b96f45930bb46a28a1368d4bdfb5c05378fa834aca616e1')
    classifier = model('LISTENING_VOICE_MODEL', 'dd054410fa4f9fcb0086b4223a9a5c66dd098c5d4cef485c03af0b0500443b73')
    audio, sr = librosa.load('/proc/self/fd/' + str(fd), sr=16000, mono=True, duration=1201)
    if len(audio) < 512 or len(audio) > 16000 * 1200:
        raise ValueError('audio length outside bounds')
    # Pad final waveform samples so the last analysis window covers all audio.
    padding = (-(len(audio) - 512)) % 256
    if padding:
        audio = np.pad(audio, (0, padding))
    mel = librosa.feature.melspectrogram(y=audio, sr=sr, n_fft=512, hop_length=256, n_mels=96,
                                        window='hann', center=False, power=2.0, norm='slaney', htk=False)
    mel = np.log10(1 + 10000 * mel)
    # Include the final partial patch, padding missing frames with silence.
    padding = (-mel.shape[1]) % 187
    if padding:
        mel = np.pad(mel, ((0, 0), (0, padding)))
    total = np.zeros(2, dtype=np.float64)
    count = 0
    # Small inference batches cap peak memory and avoid retaining all patches.
    for start in range(0, mel.shape[1], 187 * 32):
        patches = [mel[:, i:i+187].T for i in range(start, min(start + 187 * 32, mel.shape[1]), 187)]
        embeddings = extractor.run(['embeddings'], {'melspectrogram': np.asarray(patches, dtype=np.float32)})[0]
        scores = classifier.run(['activations'], {'embeddings': embeddings})[0]
        if scores.shape != (len(patches), 2) or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
            raise ValueError('invalid model output')
        total += scores.sum(axis=0)
        count += len(patches)
    if not count:
        raise ValueError('no audio patches')
    print(json.dumps((total / count).tolist()))




def _acoustid_key_directory(create=False):
    path = online._config_dir()
    try:
        if create:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise online.ConnectionError('AcoustID credential directory must be private and owned by you')
        return fd
    except FileNotFoundError:
        if not create:
            return None
        raise online.ConnectionError('Could not create AcoustID credential directory') from None
    except OSError:
        raise online.ConnectionError('Could not open the private AcoustID credential directory') from None


def _valid_acoustid_key(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value))


def acoustid_client_key():
    """Return configured application key; environment takes precedence."""
    value = os.environ.get('ACOUSTID_CLIENT_KEY', '')
    if value:
        if not _valid_acoustid_key(value):
            raise online.ConnectionError('The AcoustID application key is invalid')
        return value
    directory = _acoustid_key_directory()
    if directory is None:
        return ''
    try:
        try:
            fd = os.open('acoustid-client-key', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        except FileNotFoundError:
            return ''
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 200:
                raise online.ConnectionError('AcoustID key must be a private regular file owned by you')
            value = os.read(fd, 201).decode('ascii')
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise online.ConnectionError('Could not read the private AcoustID application key') from None
    finally:
        os.close(directory)
    if not _valid_acoustid_key(value):
        raise online.ConnectionError('The saved AcoustID application key is invalid')
    return value


def configure_acoustid_client_key(value):
    """Store an application key atomically, or clear it with an empty string."""
    if not isinstance(value, str) or (value and not _valid_acoustid_key(value)):
        raise online.ConnectionError('Enter an AcoustID application key without spaces')
    directory = _acoustid_key_directory(create=bool(value))
    if directory is not None:
        name = '.acoustid-' + secrets.token_hex(16)
        try:
            if value:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(value.encode('ascii'))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(name, 'acoustid-client-key', src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    try:
                        os.unlink(name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            else:
                try:
                    os.unlink('acoustid-client-key', dir_fd=directory)
                except FileNotFoundError:
                    pass
        except OSError:
            raise online.ConnectionError('Could not save the private AcoustID application key') from None
        finally:
            os.close(directory)
    return {'configured': bool(acoustid_client_key())}


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--classify-fd' or not sys.argv[2].isdigit():
        raise SystemExit(2)
    _classifier_worker(int(sys.argv[2]))
