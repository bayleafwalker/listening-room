"""One local listening session shared by the GTK app, desktop and remote controls.

The durable file contains a return offer, never an instruction to autoplay. Long
loads run on a worker; Quiet and Stop invalidate them before touching players.
This module deliberately has no GTK or D-Bus dependency, so its ownership and
failure rules can be tested independently of a graphical session.
"""
import concurrent.futures
import copy
import datetime
import hashlib
import inspect
import json
import math
import os
import re
from pathlib import Path
import threading
import time

import listening_hub as hub
import ambience
import audio_layers
import mpv_ipc
import room_catalogue
import station_engine
import metadata_store


class _CombinedCancel:
    """Cancel a staged player for either room cancellation or review revocation."""
    def __init__(self, room, review):
        self.room = room
        self.review = review

    def is_set(self):
        return self.room.is_set() or self.review.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = .05 if deadline is None else min(.05, max(0, deadline - time.monotonic()))
            if remaining <= 0:
                break
            self.review.wait(remaining)
        return self.is_set()


class SessionError(ValueError):
    pass


def level(value, default=50):
    if value is None:
        return default
    if isinstance(value, bool):
        raise SessionError('Choose a level from 0 to 100')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SessionError('Choose a level from 0 to 100') from None
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise SessionError('Choose a level from 0 to 100')
    return value


def _copy(value):
    return copy.deepcopy(value)


class Backend:
    """Small adapter to existing owned players; never adopts a random MPRIS app."""
    def __init__(self):
        self.remote = None
        self._radio_socket = None

    def layers(self):
        # One connection per player and one state.json read for the whole
        # refresh; state.json is rewritten only when the radio audio changed.
        with mpv_ipc.shared():
            return self._layers()

    def _layers(self):
        state = hub.state()
        self._radio_socket = hub.radio_socket(state)
        music = hub.radio_info(state)
        audio = hub.radio_audio_state(current=state)
        music = dict(music or {}, **audio)
        music.update(source_id=state.get('radio_source_id'), name=state.get('radio_name'),
                     url=state.get('radio_url') or state.get('radio_source_url'))
        music.setdefault('status', 'Stopped')
        if self.remote:
            import online_services
            player = next((p for p in online_services.ma_players() if p['id'] == self.remote['player_id']), None)
            if player:
                music = dict(self.remote, id='music-assistant', kind='music_assistant',
                             status={'playing': 'Playing', 'paused': 'Paused'}.get(player['state'], 'Stopped'),
                             volume=player.get('volume'), muted=False, title=self.remote.get('name', ''),
                             name=player['name'])
            else:
                music = dict(self.remote, kind='music_assistant', status='Stopped', volume=None)
        atmosphere = audio_layers.state() or dict(audio_layers.audio_settings(), status='Stopped')
        picture = ambience.state('ambience') or {'status': 'Stopped'}
        return {'music': music, 'atmosphere': atmosphere, 'picture': picture}

    def start_music(self, source, cancel):
        if source.get('kind') == 'local':
            import atmosphere_catalogue
            local = atmosphere_catalogue.source(source.get('source_id') or source.get('id'))
            source = dict(source, url=local['url'], kind='radio', mpv_options=['--loop-file=inf'])
        reference = source.get('reference') or {}
        if reference.get('provider') == 'music_assistant':
            import online_services
            if cancel.is_set():
                raise SessionError('Room entry cancelled')
            if reference.get('radio_mode') is True:
                online_services.ma_play(reference.get('uri'), reference.get('player_id'), radio_mode=True)
            else:
                online_services.ma_play(reference.get('uri'), reference.get('player_id'))
            if cancel.is_set():
                online_services.ma_command('players/cmd/stop', {'player_id': reference.get('player_id')})
                raise SessionError('Room entry cancelled')
            self.remote = dict(reference)
            if hub.radio_info():
                hub.player_command('radio', 'stop')
            return dict(source, id='music-assistant', kind='music_assistant')
        if source.get('reference'):
            import online_services
            source = online_services.prepare_playback(source['reference'], cancel_event=cancel)
        if cancel.is_set():
            raise SessionError('Room entry cancelled')
        kind = source.get('kind', 'radio')
        if kind == 'radio':
            kwargs = {}
            parameters = inspect.signature(hub.start_radio).parameters
            if 'cancel_event' in parameters:
                kwargs['cancel_event'] = cancel
            if 'fade_seconds' in parameters:
                kwargs['fade_seconds'] = .65
            options = list(source.get('mpv_options') or [])
            if source.get('reference', {}).get('provider') == 'jellyfin':
                # Retain the completed file long enough to observe a positive
                # eof-reached property. A disappeared player is never evidence
                # that a library track finished normally.
                options.append('--keep-open=yes')
            if 'mpv_options' in parameters and options:
                kwargs['mpv_options'] = options
            if source.get('resolver') or source.get('source_id') in ('twitch-channel', 'youtube-video'):
                import shutil
                kwargs['ytdlp'] = shutil.which('yt-dlp')
                if not kwargs['ytdlp']:
                    raise SessionError('The public video resolver is unavailable')
            hub.start_radio(source, **kwargs)
            if self.remote:
                import online_services
                online_services.ma_command('players/cmd/stop', {'player_id': self.remote['player_id']})
                self.remote = None
        else:
            raise SessionError('Choose a direct radio, public video or library item for room music')
        return source

    def start_verified_music(self, source, cancel, audio_file, player_unit=None):
        reference = source.get('reference') or {}
        source = dict(source, id='jellyfin-' + reference['id'], name=reference.get('name', 'Library track'), kind='radio')
        hub.start_radio(source, cancel_event=cancel, fade_seconds=.65,
                        mpv_options=['--keep-open=yes'], trusted_fd=audio_file.fileno(), trusted_unit=player_unit)
        return source

    def stop_owned_identity(self, player_unit):
        if not isinstance(player_unit, str) or not re.fullmatch(r'listening-hub-radio-[0-9a-f]{12}\.service', player_unit):
            raise SessionError('Invalid owned player identity')
        if not mpv_ipc.ensure_stopped(hub.run, player_unit):
            raise SessionError('Could not stop an orphaned verified player')

    def music_control(self, action, value=None):
        if self.remote:
            import online_services
            if action == 'muted':
                return
            command = {'pause': 'pause', 'play': 'play', 'stop': 'stop', 'volume': 'volume_set'}.get(action)
            if action in ('toggle', 'play-pause'):
                state = self.layers()['music'].get('status')
                command = 'pause' if state == 'Playing' else 'play'
            if action == 'mute':
                raise SessionError('Use this remote player’s volume control to mute it')
            if not command:
                raise SessionError('This remote player control is unavailable')
            args = {'player_id': self.remote['player_id']}
            if action == 'volume':
                args['volume_level'] = round(level(value))
            return online_services.ma_command('players/cmd/' + command, args)
        if action in ('pause', 'play'):
            hub.ipc(['set_property', 'pause', action == 'pause'])
        elif action in ('toggle', 'play-pause'):
            hub.ipc(['cycle', 'pause'])
        elif action == 'volume':
            hub.set_volume(str(round(level(value))), target_id='radio')
        elif action == 'mute':
            hub.mute(target_id='radio')
        elif action == 'muted':
            hub.ipc(['set_property', 'mute', bool(value)])
        elif action == 'stop':
            hub.player_command('radio', 'stop')
        elif action == 'seek':
            hub.ipc(['seek', float(value), 'relative'])
        else:
            raise SessionError('This music control is unavailable')

    def start_atmosphere(self, selection, cancel):
        kind = selection.get('kind', 'none')
        actual = None
        if kind == 'none':
            audio_layers.control('stop')
            return
        if kind == 'local':
            audio_layers.start_local(selection.get('source_id') or selection.get('id'),
                                     volume=selection.get('volume'), cancel_event=cancel,
                                     window_open=selection.get('window_open', 35))
        elif kind == 'twitch' and not selection.get('reference'):
            audio_layers.start_layer(selection.get('url'), volume=selection.get('volume'), cancel_event=cancel)
        elif kind in ('radio', 'twitch') or selection.get('reference'):
            source = dict(selection)
            if not source.get('url') and not source.get('reference'):
                source = self._direct_source(source)
            actual = audio_layers.start_source(source, volume=selection.get('volume'), cancel_event=cancel)
        else:
            raise SessionError('Choose a local sound or a public Twitch channel')
        live = audio_layers.state() or {}
        if bool(live.get('muted')) != bool(selection.get('muted')):
            audio_layers.control('mute')
        return actual

    def atmosphere_details(self):
        with mpv_ipc.shared():
            return audio_layers.media_details()

    @staticmethod
    def _direct_source(selection):
        source = next((s for s in hub.sources() if s['id'] == selection.get('source_id')), None)
        if not source or source.get('kind') != 'radio':
            raise SessionError('Choose a direct audio source')
        return dict(source, **selection)

    def atmosphere_control(self, action, value=None):
        return audio_layers.control(action, value)

    def start_picture(self, settings, cancel=None):
        if cancel is not None and cancel.is_set():
            raise SessionError('Picture opening cancelled')
        if not settings.get('enabled', True):
            if ambience.state('ambience'):
                ambience.control('stop', 'ambience')
            return
        preset = next((p for p in ambience.presets() if p['id'] == settings.get('preset_id')), None)
        if preset is None and settings.get('url') and hub.valid_url(settings['url']):
            import online_services
            descriptor = online_services.preview_link(settings['url'])
            if 'picture' not in descriptor['actions']:
                raise SessionError('This link must be opened in its original service')
            preset = {'id': 'pasted-link', 'name': descriptor['name'], 'url': descriptor['url']}
        if not preset:
            raise SessionError('Picture unavailable; choose another view or sound only')
        if settings.get('still'):
            if not preset.get('still_url') or not Path(preset['still_url']).is_file():
                raise SessionError('Still artwork is unavailable for this view')
            preset = dict(preset, url=preset['still_url'], name=preset['name'] + ' · still artwork')
        kwargs = {'mode': 'ambience', 'placement': settings.get('placement', '9')}
        if 'cancel_event' in inspect.signature(ambience.start).parameters:
            kwargs['cancel_event'] = cancel
        if 'display' in inspect.signature(ambience.start).parameters and settings.get('display'):
            kwargs['display'] = settings['display']
        result = ambience.start(preset, **kwargs)
        if settings.get('display'):
            ambience.place_on_display(settings['display'])
        return result

    def picture_control(self, action, value=None):
        if action == 'placement':
            return ambience.place(value, 'ambience')
        return ambience.control(action, 'ambience')

    def extra_video_control(self, action):
        if ambience.state('video'):
            ambience.control(action, 'video')

    def media_details(self):
        if self.remote:
            return {}
        details = {}
        # The socket the same refresh's layers() read, so no second state.json read.
        path = self._radio_socket
        with mpv_ipc.shared():
            for key, prop in (('position', 'time-pos'), ('duration', 'duration'), ('seekable', 'seekable'),
                              ('eof_reached', 'eof-reached')):
                try:
                    details[key] = hub.ipc(['get_property', prop], timeout=.3, socket_path=path)
                except (OSError, ValueError, hub.HubError):
                    pass
        return details


class Controller:
    def __init__(self, backend=None, directory=None, clock=time.time):
        self.backend = backend or Backend()
        self.directory = Path(directory or hub.STATE)
        self.clock = clock
        self.lock = threading.RLock()
        self.worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='room-session')
        stored = hub.load_json(self.directory / 'session.json', {})
        self.last_session = stored.get('last_session') if isinstance(stored, dict) else None
        self.settings = dict(placement='9', still=False, reduced_motion=False,
                             pause_on_disconnect=True, duck_on_call=False, keep_controls=False)
        self.settings.update(stored.get('settings') or {} if isinstance(stored, dict) else {})
        # Session-wide multiplier (percent) over the Music and Ambience levels.
        # It never changes a layer's own level or mute, so the balance is kept.
        self.room_volume = 100.
        saved_volume = stored.get('room_volume') if isinstance(stored, dict) else None
        if isinstance(saved_volume, (int, float)) and not isinstance(saved_volume, bool) and 0 <= saved_volume <= 100:
            self.room_volume = float(saved_volume)
        self.last_entered_at = stored.get('last_entered_at') if isinstance(stored, dict) else None
        self.timer_ends = {}  # running wind-down / dim timers -> controller-clock end time
        self.recent = (stored.get('recent') or [])[:20] if isinstance(stored, dict) else []
        self.active = None
        self.live = {name: {'status': 'Stopped'} for name in ('music', 'atmosphere', 'picture')}
        self.errors = {}
        self.transition = None
        self.resume_offer = None
        self.generation = 0
        self.cancel = threading.Event()
        self.closed = False
        self.timer = None
        self.ducked = None
        self.duck_manual = set()
        self.policy_status = {}
        self._file_cache = {}
        self.media = {}
        self.callbacks = []
        self.lighting_timer = None
        self.lighting_generation = 0
        self.auto_advance_pending = False
        self.eof_attempted = None
        self.reconnect_attempted = False
        self.playing_since = None
        self.atmosphere_generation = 0
        self.atmosphere_eof_attempted = None
        self.atmosphere_pending = False
        self.program = None
        self.strict_pending = None
        self.verified_audio = None
        self.verified_record = None
        self.verified_digest = None
        self.pending_revocation_stop = None
        self.metadata_store = metadata_store.MetadataStore(self.directory / 'metadata.sqlite3')
        self.metadata_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='room-metadata')
        self.analysis_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='room-analysis')
        self.metadata_jobs = {}
        self.station_history = (stored.get('station_history') or [])[:20] if isinstance(stored, dict) else []
        self.station_memory = (stored.get('station_memory') or {}) if isinstance(stored, dict) and isinstance(stored.get('station_memory') or {}, dict) else {}
        self.recovery_blocked = False
        self.verified_units = []
        marker_path = self.directory / 'verified-playback.json'
        try:
            with marker_path.open('rb') as marker_file:
                raw_marker = marker_file.read(65537)
            if len(raw_marker) > 65536:
                raise ValueError('Recovery marker is too large')
            saved_units = json.loads(raw_marker)
            if not isinstance(saved_units, list) or len(saved_units) > 20:
                raise ValueError('Invalid recovery marker')
            seen = set()
            for entry in saved_units:
                if (not isinstance(entry, dict) or set(entry) != {'unit', 'origin', 'item_id', 'audio_sha256'}
                        or not isinstance(entry['unit'], str)
                        or not re.fullmatch(r'listening-hub-radio-[0-9a-f]{12}\.service', entry['unit'])
                        or entry['unit'] in seen):
                    raise ValueError('Invalid recovery marker')
                metadata_store._key(entry['origin'], entry['item_id'], entry['audio_sha256'])
                seen.add(entry['unit'])
            self.verified_units = saved_units
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.recovery_blocked = True
            self.errors['music'] = 'Verified playback recovery marker is unreadable or invalid'
        if self.verified_units:
            self.worker.submit(self._recover_verified_players)

    def _save_verified_units(self):
        hub.save_json(self.directory / 'verified-playback.json', self.verified_units)

    def _recover_verified_players(self, entries=None):
        for entry in list(self.verified_units if entries is None else entries):
            try:
                self.backend.stop_owned_identity(entry['unit'])
            except (OSError, ValueError, hub.HubError):
                with self.lock:
                    self.recovery_blocked = True
                    self.errors['music'] = 'Could not stop an orphaned verified player'
                continue
            with self.lock:
                if entry in self.verified_units:
                    previous = list(self.verified_units)
                    self.verified_units.remove(entry)
                    try:
                        self._save_verified_units()
                    except (OSError, ValueError):
                        self.verified_units = previous
                        self.recovery_blocked = True
                        self.errors['music'] = 'Could not update verified playback recovery marker'



    def _check_pending_review(self):
        pending = self.strict_pending
        if pending:
            current = self.metadata_store.inspect(*pending['record'], pending['digest'])
            if not current or (current.get('review') or {}).get('classification') != 'instrumental':
                pending['event'].set()
        if self.verified_record and self.verified_digest:
            current = self.metadata_store.inspect(*self.verified_record, self.verified_digest)
            if not current or (current.get('review') or {}).get('classification') != 'instrumental':
                target, expected = self.verified_record, self.generation
                marker = (expected, target, self.verified_digest)
                if self.pending_revocation_stop == marker or self.closed:
                    return
                self.pending_revocation_stop = marker
                def stop_revoked():
                    with self.lock:
                        if expected != self.generation or self.verified_record != target or self.verified_digest != marker[2]:
                            return
                    try:
                        self.backend.music_control('stop')
                    finally:
                        with self.lock:
                            if expected == self.generation and self.verified_record == target and self.verified_digest == marker[2]:
                                if self.verified_audio:
                                    self.verified_audio.close()
                                self.verified_audio = None
                                self.verified_record = None
                                self.verified_digest = None
                                self.program = None
                                self.errors['music'] = 'Instrumental review revoked or recording changed'
                        self._changed()
                self.worker.submit(stop_revoked)

    def _metadata_observe(self, *args, **kwargs):
        with self.lock:
            result = self.metadata_store.observe(*args, **kwargs)
            self._check_pending_review()
            return result

    def _metadata_cached(self, item):
        if not isinstance(item, dict) or item.get('provider') != 'jellyfin' or item.get('type') != 'Audio':
            return None
        try:
            import metadata_pipeline
            return self.metadata_store.current(metadata_pipeline.library_origin(), item.get('id'))
        except (ValueError, OSError, RuntimeError, ImportError):
            return None

    def _metadata_needs_refresh(self, item):
        record = self._metadata_cached(item)
        if not record:
            return True
        try:
            age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(record['updated_at'])
            return age.total_seconds() > 7 * 86400
        except (ValueError, KeyError, TypeError):
            return True

    def _schedule_analysis(self, candidates):
        for candidate in candidates[:8]:
            if station_engine.instrumental_score(self._metadata_cached(candidate)) is None:
                with self.lock:
                    pending = any(job['kind'] == 'analyze' and job['status'] in ('queued', 'running') and job.get('_item_id') == candidate.get('id') for job in self.metadata_jobs.values())
                if not pending:
                    try:
                        self._metadata_job('analyze', {'reference': candidate})
                    except (ValueError, ImportError):
                        break

    def _metadata_job(self, kind, args):
        import metadata_pipeline
        with self.lock:
            if sum(j['status'] in ('queued', 'running') for j in self.metadata_jobs.values()) >= 20:
                raise SessionError('Too many metadata jobs')
            job_id = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
            event = threading.Event()
            job = {'job_id': job_id, 'kind': kind, 'status': 'queued', 'message': '', '_cancel': event, '_item_id': (args.get('reference') or {}).get('id') if isinstance(args.get('reference'), dict) else None, '_record': (args.get('origin'), args.get('item_id')) if kind == 'review' else None}
            self.metadata_jobs[job_id] = job
            while len(self.metadata_jobs) > 100:
                oldest = next((key for key, value in self.metadata_jobs.items() if value['status'] not in ('queued', 'running')), None)
                if oldest is None:
                    break
                del self.metadata_jobs[oldest]
        def work():
            with self.lock:
                job['status'] = 'running'
            try:
                if event.is_set() or self.closed:
                    raise InterruptedError()
                if kind == 'enrich':
                    reference = args.get('reference')
                    if not isinstance(reference, dict) or reference.get('provider') != 'jellyfin' or reference.get('type') != 'Audio':
                        raise ValueError('Choose a library audio item')
                    observation = metadata_pipeline.collect_jellyfin(reference, cancel_event=event)
                    if event.is_set() or self.closed:
                        raise InterruptedError()
                    self._metadata_observe(observation['origin'], observation['item_id'], None,
                                                observation.get('identities'), observation.get('features'), observation.get('revision'))
                elif kind in ('match', 'analyze'):
                    reference = args.get('reference')
                    if not isinstance(reference, dict) or reference.get('provider') != 'jellyfin' or reference.get('type') != 'Audio':
                        raise ValueError('Choose a library audio item')
                    origin = metadata_pipeline.library_origin()
                    observation = metadata_pipeline.collect_jellyfin(reference, cancel_event=event)
                    if origin != observation['origin']:
                        raise ValueError('Library changed')
                    if kind == 'match':
                        if args.get('method', 'musicbrainz') == 'acoustid':
                            client_key = metadata_pipeline.acoustid_client_key()
                            if not client_key:
                                raise SessionError('Configure an AcoustID application key first')
                            fingerprint = metadata_pipeline.chromaprint_jellyfin(reference, cancel_event=event)
                            candidates = metadata_pipeline.acoustid_lookup(fingerprint['fingerprint'], fingerprint['duration'], client_key, cancel_event=event)
                        elif args.get('method', 'musicbrainz') == 'musicbrainz':
                            candidates = metadata_pipeline.musicbrainz_search(reference, cancel_event=event)
                        else:
                            raise ValueError('Unknown matching method')
                        if event.is_set() or self.closed:
                            raise InterruptedError()
                        candidates = [{'provider': x['provider'], 'provider_id': str(x['provider_id'])[:80],
                                       'title': (hub.clean(str(x.get('title') or ''))[:200] if not any(c in str(x.get('title') or '') for c in ('/', chr(92))) else 'Recording candidate'), 'status': 'candidate'}
                                      for x in candidates[:20] if isinstance(x, dict) and x.get('provider') in ('MusicBrainz', 'AcoustID') and isinstance(x.get('provider_id'), str)]
                        self._metadata_observe(origin, observation['item_id'], None, observation.get('identities'), observation.get('features'), observation.get('revision'))
                        with self.lock:
                            job['_record'] = (origin, observation['item_id'])
                            job['_revision'] = observation.get('revision')
                            job['result'] = {'candidates': candidates}
                    else:
                        result = metadata_pipeline.classify_jellyfin(reference, cancel_event=event)
                        if event.is_set() or self.closed:
                            raise InterruptedError()
                        if not isinstance(result, dict):
                            raise ValueError('Invalid analysis')
                        if not result.get('audio_sha256'):
                            self._metadata_observe(origin, observation['item_id'], None, observation.get('identities'), observation.get('features'), observation.get('revision'))
                            self.metadata_store.observe_unbound(origin, observation['item_id'], result.get('features', []))
                            with self.lock:
                                job['result'] = {'features': result.get('features', []), 'binding': 'unknown'}
                        else:
                            if not result.get('revision') or result['revision'] != observation.get('revision') or result.get('origin') != origin or origin != metadata_pipeline.library_origin():
                                raise ValueError('Recording changed during analysis')
                            current = self.metadata_store.inspect(origin, observation['item_id'], result['audio_sha256'])
                            if not current:
                                self._metadata_observe(origin, observation['item_id'], result['audio_sha256'], observation.get('identities'), observation.get('features', []) + result.get('features', []), result['revision'])
                            else:
                                self._metadata_observe(origin, observation['item_id'], result['audio_sha256'], current['identities'], [x for x in current['features'] if not (x.get('provider') == 'Essentia MTG' and x.get('name') in ('instrumental score', 'voice score'))] + result.get('features', []), result['revision'])
                            with self.lock:
                                job['result'] = {'features': result.get('features', []), 'binding': 'audio_sha256'}
                else:
                    origin, item_id = args.get('origin'), args.get('item_id')
                    if origin != metadata_pipeline.library_origin():
                        raise ValueError('Library changed')
                    reference = {'provider': 'jellyfin', 'id': item_id, 'type': 'Audio'}
                    before = metadata_pipeline.collect_jellyfin(reference, cancel_event=event)
                    digest = metadata_pipeline.fingerprint_jellyfin(reference, cancel_event=event)
                    after = metadata_pipeline.collect_jellyfin(reference, cancel_event=event)
                    if event.is_set() or self.closed:
                        raise InterruptedError()
                    if before['origin'] != origin or after['origin'] != origin or not before.get('revision') or before.get('revision') != after.get('revision'):
                        raise ValueError('Recording changed')
                    with self.lock:
                        if event.is_set() or self.closed:
                            raise InterruptedError()
                        self._metadata_observe(origin, item_id, digest, after.get('identities'), after.get('features'), after.get('revision'))
                        self.metadata_store.review(origin, item_id, digest, args.get('classification'), args.get('reviewer'))
                        self._check_pending_review()
                with self.lock:
                    if kind in ('enrich', 'analyze') and self.program:
                        self.program.retune()
                    job.update(status='completed', message='Metadata updated')
                    if (kind == 'enrich' and self.program and self.program.station['preferences'].get('speech') == 'Likely instrumental'
                            and any(x.get('id') == args.get('reference', {}).get('id') for x in self.program.candidates[:8])):
                        self._schedule_analysis([args['reference']])
            except Exception:
                with self.lock:
                    job.update(status='cancelled' if event.is_set() or self.closed else 'failed',
                               message='Metadata job cancelled' if event.is_set() or self.closed else ('Analysis unavailable; configure a supported classifier and model' if kind == 'analyze' else 'Metadata job failed; check the library connection and current recording'))
            self._changed()
        (self.analysis_worker if kind == 'analyze' else self.metadata_worker).submit(work)
        return {'accepted': True, 'job_id': job_id}

    def _persist(self):
        hub.save_json(self.directory / 'session.json', {'version': 1, 'last_session': self.last_session,
                       'settings': self.settings, 'recent': self.recent, 'station_history': self.station_history,
                       'station_memory': self.station_memory, 'room_volume': self.room_volume,
                       'last_entered_at': self.last_entered_at})

    def _timers(self):
        timers = dict(self.timer_ends)
        offer = self.resume_offer
        if offer and not offer.get('ready'):
            timers['break'] = offer['ready_at']
        return timers

    def _effective(self, value):
        """Level a player receives: the layer's own level scaled by room volume."""
        if self.room_volume >= 100 or value is None:
            return value
        return value * self.room_volume / 100

    def _scaled(self, selection):
        if self.room_volume >= 100 or selection.get('volume') is None:
            return selection
        return dict(selection, volume=self._effective(selection['volume']))

    def _level_of(self, name, live_volume):
        """The layer's own level behind a player volume that room volume has scaled."""
        if live_volume is None or self.room_volume >= 100:
            return live_volume
        stored = ((self.active or {}).get(name) or {}).get('volume')
        if stored is not None and abs(live_volume - self._effective(stored)) < 1:
            return stored
        if self.room_volume <= 0:
            return stored if stored is not None else live_volume
        return min(100., live_volume * 100 / self.room_volume)

    def set_room_volume(self, value=None, delta=None):
        """Change the multiplier only; layer levels, mutes and epochs stay untouched."""
        with self.lock:
            if value is None:
                if delta is None:
                    raise SessionError('Choose a room volume from 0 to 100')
                if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta):
                    raise SessionError('Choose a room volume change such as +5')
                value = max(0., min(100., self.room_volume + delta))
            self.room_volume = level(value)
            if self.active:
                self.active['room_volume'] = self.room_volume
                self.last_session = _copy(self.active)
            self._persist()
            live = _copy(self.live)
            for name in ('music', 'atmosphere'):
                if (live.get(name) or {}).get('status') not in ('Playing', 'Paused'):
                    continue
                stored = ((self.active or {}).get(name) or {}).get('volume')
                if stored is None:
                    continue
                duck = (self.ducked or {}).get(name)
                if duck and duck['identity'] == self._layer_identity(name):
                    duck['volume'] = self._effective(stored)
                    target = duck['volume'] * .2
                else:
                    target = self._effective(stored)
                control = self.backend.music_control if name == 'music' else self.backend.atmosphere_control
                try:
                    control('volume', target)
                except (OSError, ValueError, hub.HubError) as error:
                    self.errors[name] = str(error)
                else:
                    self.live[name]['volume'] = target
        self._changed()
        return {'message': 'Room volume set', 'room_volume': self.room_volume}

    def stations(self):
        values = hub.load_json(self.directory / 'stations.json', [])
        result = []
        for value in values if isinstance(values, list) else []:
            try:
                result.append(station_engine.normalize(value))
            except (ValueError, TypeError):
                continue
        return result

    def variants(self):
        values = hub.load_json(self.directory / 'room-variants.json', [])
        return [v for v in values if isinstance(v, dict) and isinstance(v.get('id'), str)
                and isinstance(v.get('name'), str) and isinstance(v.get('music'), dict)] if isinstance(values, list) else []

    def _cached_file(self, name, load):
        """`load()` for a saved file, re-reading only after the file changed.

        The key is mtime, size and inode (saves replace the file atomically). A
        file modified within the last two seconds is not trusted yet, because
        mtime is coarse; it is read again until it has settled.
        """
        try:
            info = (self.directory / name).stat()
            key = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            key = None
        cached = self._file_cache.get(name)
        if cached and cached[0] == key and cached[1]:
            return cached[2]
        value = load()
        self._file_cache[name] = (key, key is None or time.time_ns() - key[0] > 2_000_000_000, value)
        return value

    def snapshot(self):
        with self.lock:
            return _copy({'active': self.active, 'last_session': self.last_session,
                          'variants': self._cached_file('room-variants.json', self.variants), 'layers': self.live, 'settings': self.settings,
                          'recent': self.recent, 'resume_offer': self.resume_offer,
                          'transition': self.transition, 'errors': self.errors,
                          'media': self.media, 'audio_policy': self.policy_status,
                          'room_volume': self.room_volume, 'last_entered_at': self.last_entered_at,
                          'now': self.clock(), 'timers': self._timers(),
                          'levels': {name: self._level_of(name, (self.live.get(name) or {}).get('volume'))
                                     for name in ('music', 'atmosphere')},
                          'stations': self._cached_file('stations.json', self.stations),
                          'station_recent': self.station_history,
                          'station': ({'definition': self.program.station, 'upcoming': ([self.program.override] if self.program.override else []) + self.program.queue,
                                       'recent': self.program.recent,
                                       'current': (self.active or {}).get('music', {}).get('reference'),
                                       'explanation': self.program.explain(self.program.recent[-1]) if self.program.recent else [],
                                       'hold': self.program.hold,
                                       'can_next': self.program.can_next(),
                                       'can_previous': False} if self.program else
                                      ({'definition': self.active['music']['station'], 'upcoming': [],
                                        'recent': [], 'current': self.active['music'].get('reference'),
                                        'can_next': False, 'can_previous': False, 'hold': False}
                                       if self.active and self.active.get('music', {}).get('station') else None))})

    def _changed(self):
        for callback in self.callbacks:
            try:
                callback()
            except Exception:
                pass

    def refresh(self):
        """Called off UI/D-Bus threads. Snapshot readers never wait for IPC."""
        if self.closed:
            return
        observed_generation = self.generation
        try:
            layers = self.backend.layers()
            previous_status = (self.live.get('music') or {}).get('status')
            music = layers.get('music') or {}
            with self.lock:
                if observed_generation != self.generation:
                    return
                if music.get('status') == 'Playing':
                    if self.playing_since is None:
                        self.playing_since = self.clock()
                    elif self.clock() - self.playing_since >= 60:
                        self.reconnect_attempted = False
                else:
                    self.playing_since = None
            selected = (self.active or {}).get('music') or {}
            if music.get('status') != 'Stopped' and selected.get('reference', {}).get('provider') == 'jellyfin':
                queue = selected.get('queue') or []
                index = selected.get('queue_index', 0)
                track = queue[index] if isinstance(index, int) and 0 <= index < len(queue) else selected['reference']
                music.update(title=track.get('name') or music.get('title'), artist=track.get('artist', ''),
                             album=track.get('album', ''), station='')
            elif music.get('kind') != 'music_assistant':
                music['station'] = music.get('name') or music.get('artist', '')
                # The original radio adapter uses artist for the station name;
                # it is not evidence identifying the artist of a recording.
                if music.get('artist') == music.get('station'):
                    music['artist'] = ''
            with self.lock:
                if observed_generation != self.generation:
                    return
                self.live = layers
            if hasattr(self.backend, 'media_details') and self.active:
                details = self.backend.media_details()
            else:
                details = {}
            with self.lock:
                if observed_generation != self.generation:
                    return
                self.media = details
            atmosphere = (self.active or {}).get('atmosphere') or {}
            atmosphere_queue = atmosphere.get('queue') or []
            atmosphere_index = atmosphere.get('queue_index', 0)
            if isinstance(atmosphere_index, int) and 'atmosphere' in layers:
                layers['atmosphere']['can_next'] = atmosphere_index + 1 < len(atmosphere_queue) and not self.atmosphere_pending
            if atmosphere.get('reference', {}).get('provider') == 'jellyfin' and hasattr(self.backend, 'atmosphere_details'):
                details = self.backend.atmosphere_details()
                with self.lock:
                    identity = (atmosphere.get('source_id'), atmosphere.get('queue_index', 0))
                    if (details.get('eof_reached') is True and observed_generation == self.generation
                            and (layers.get('atmosphere') or {}).get('source_id') == identity[0]
                            and not self.transition and not self.resume_offer and not self.atmosphere_pending
                            and identity != self.atmosphere_eof_attempted):
                        self.atmosphere_eof_attempted = identity
                        self._atmosphere_next(automatic=True)
            with self.lock:
                selected = (self.active or {}).get('music') or {}
                queue = selected.get('queue') or []
                if (selected.get('station') and previous_status == 'Playing'
                        and music.get('status') == 'Stopped' and observed_generation == self.generation
                        and self.media.get('eof_reached') is not True
                        and not self.transition and not self.resume_offer and not self.reconnect_attempted):
                    self.reconnect_attempted = True
                    recipe = _copy(self.active)
                    recipe['music']['_station_continuation'] = True
                    recipe['music']['_station_reconnect'] = True
                    recipe['music']['position'] = 0
                    self.enter({'recipe': recipe, '_automatic': True, '_request_generation': observed_generation})
                    return
                eof_identity = (layers.get('music', {}).get('source_id'),
                                selected.get('queue_index', 0), repr(selected.get('reference')))
                if (self.media.get('eof_reached') is True
                        and selected.get('reference', {}).get('provider') == 'jellyfin'
                        and layers.get('music', {}).get('source_id') == selected.get('source_id')
                        and observed_generation == self.generation
                        and (self.program is not None or int(selected.get('queue_index', 0)) + 1 < len(queue))
                        and eof_identity != self.eof_attempted
                        and not self.transition and not self.resume_offer and not self.auto_advance_pending):
                    self.auto_advance_pending = True
                    self.eof_attempted = eof_identity
                    self.queue_step(1, automatic=True)
        except (OSError, ValueError, hub.HubError) as error:
            with self.lock:
                self.errors['refresh'] = str(error)
        with self.lock:
            if self.resume_offer and not self.resume_offer.get('ready'):
                if self.clock() >= self.resume_offer['ready_at']:
                    self.resume_offer['ready'] = True
        self._changed()

    def recipe(self, args):
        identity = args.get('variant_id') or args.get('room_id')
        saved = next((v for v in self.variants() if v['id'] == identity), None)
        if saved:
            recipe = _copy(saved)
        elif identity:
            room = next((r for r in room_catalogue.rooms() if r['id'] == identity), None)
            if not room:
                raise SessionError('Room not found')
            recipe = {'id': room['id'], 'room_id': room['id'], 'name': room['name'],
                      'poster_path': room.get('poster_path'), 'accent': room.get('accent'),
                      'music': {'source_id': room['source_id'], 'volume': 50, 'muted': False},
                      'atmosphere': {'kind': 'none', 'volume': 20, 'muted': False, 'window_open': 35},
                      'picture': {'preset_id': room.get('view_id'), 'enabled': True,
                                  'still': self.settings['still'] or self.settings['reduced_motion'],
                                  'placement': self.settings['placement'],
                                  'display': self.settings.get('display') or self.settings.get('preferred_display')}}
        elif args.get('recipe'):
            recipe = _copy(args['recipe'])
        elif args.get('music'):
            recipe = _copy(self.active or self.last_session or {
                'id': 'custom', 'room_id': None, 'name': 'Your listening room',
                'atmosphere': {'kind': 'none', 'volume': 20, 'muted': False, 'window_open': 35},
                'picture': {'enabled': False, 'placement': self.settings['placement']}})
        else:
            raise SessionError('Choose a room first')
        for key in ('music', 'atmosphere', 'picture'):
            if key in args:
                if key == 'music' and 'station' not in args[key]:
                    for field in ('station', 'station_source_index', '_station_continuation', '_station_replay', '_station_reconnect'):
                        recipe.setdefault('music', {}).pop(field, None)
                if key == 'music' and (args[key].get('source_id') != recipe.get('music', {}).get('source_id')
                                       or ('reference' in args[key] and args[key]['reference'] != recipe.get('music', {}).get('reference'))
                                       or ('url' in args[key] and args[key]['url'] != recipe.get('music', {}).get('url'))):
                    for field in ('url', 'reference', 'collection_reference', 'queue', 'queue_index', 'position', 'resolver'):
                        recipe.setdefault('music', {}).pop(field, None)
                recipe.setdefault(key, {}).update(args[key])
        if 'with_view' in args:
            recipe['picture']['enabled'] = bool(args['with_view'])
        for key in ('still', 'placement', 'display'):
            if key in args:
                recipe['picture'][key] = args[key]
        self._validate_recipe(recipe)
        return recipe

    def _validate_recipe(self, recipe):
        if not isinstance(recipe, dict) or not isinstance(recipe.get('music'), dict):
            raise SessionError('This saved room is incomplete')
        for key in ('music', 'atmosphere'):
            selection = recipe.setdefault(key, {})
            selection['volume'] = level(selection.get('volume'), 50 if key == 'music' else 20)
            selection['muted'] = bool(selection.get('muted', False))
        if recipe.get('room_volume') is not None:
            recipe['room_volume'] = level(recipe['room_volume'], 100)
        atmosphere = recipe['atmosphere']
        atmosphere.setdefault('kind', 'none')
        atmosphere['window_open'] = level(atmosphere.get('window_open'), 35)
        picture = recipe.setdefault('picture', {'enabled': False})
        if picture.get('placement', '9') not in ('9', 'here', 'pin', 'pip'):
            raise SessionError('Choose workspace 9, Here, Pin or Picture in picture')
        recipe['name'] = hub.clean(recipe.get('name')) or 'Your listening room'

    def _source(self, recipe):
        music = recipe['music']
        if music.get('kind') == 'local':
            import atmosphere_catalogue
            atmosphere_catalogue.source(music.get('source_id') or music.get('id'))
            return dict(music, id=music.get('source_id') or music.get('id'))
        if music.get('reference'):
            return dict(music, id=music.get('source_id', 'library'), kind='radio')
        source = next((s for s in hub.sources() if s['id'] == music.get('source_id')), None)
        if source:
            source = _copy(source)
        elif music.get('url') and hub.valid_url(music['url']):
            source = dict(music, id=music.get('source_id', 'custom-stream'), kind='radio')
        else:
            raise SessionError('Music source unavailable; choose a station or library item')
        if source.get('kind') != 'radio':
            raise SessionError('This source opens another app; choose a direct source for a saved room')
        return source

    def _invalidate(self):
        with self.lock:
            self.cancel.set()
            self.cancel = threading.Event()
            self.generation += 1
            self.atmosphere_generation += 1
            self.atmosphere_pending = False
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.lighting_generation += 1
            if self.lighting_timer:
                self.lighting_timer.cancel()
                self.lighting_timer = None
            self.timer_ends.clear()
            self.transition = None
            return self.generation, self.cancel

    def enter(self, args):
        recipe = self.recipe(args)
        source = self._source(recipe)  # Validate before cancelling the previous session.
        with self.lock:
            expected = args.get('_request_generation')
            if expected is not None and expected != self.generation:
                return {'message': 'Room entry cancelled'}
            generation, cancel = self._invalidate()
            if not args.get('_automatic'):
                self.eof_attempted = None
                self.reconnect_attempted = False
                self.playing_since = None
            self.resume_offer = None
            self.errors = {}
            self.transition = {'kind': 'enter', 'name': recipe['name'], 'id': generation}
        self.worker.submit(self._enter, recipe, source, generation, cancel, args.get('_program_next'))
        self._changed()
        return {'accepted': True, 'message': 'Entering ' + recipe['name'], 'id': generation}

    def _enter(self, recipe, source, generation, cancel, program_next=None):
        strict_guard = None
        strict_started = False
        verified_file = None
        def valid():
            return not cancel.is_set() and not self.closed and generation == self.generation
        try:
            if not valid():
                return
            if self.recovery_blocked:
                raise SessionError('Could not stop an orphaned verified player; restart the room service to retry')
            previous = _copy(self.active)
            if recipe.get('audio'):
                self.apply_settings(recipe['audio'])
            station = recipe['music'].get('station')
            new_program = None
            generated_fallback = False
            fallback_offset = 0
            continuation = recipe['music'].get('_station_continuation')
            reconnecting = recipe['music'].pop('_station_reconnect', False)
            if station and not continuation:
                station = station_engine.normalize(station)
                recipe['music']['position'] = 0
                source_index = recipe['music'].get('station_source_index', 0)
                if not isinstance(source_index, int) or isinstance(source_index, bool) or not 0 <= source_index < len(station['sources']):
                    source_index = 0
                replay_items = recipe['music'].get('_station_replay')
                reference = (replay_items[0] if isinstance(replay_items, list) and replay_items else
                             station['sources'][source_index].get('reference') or {})
                if station['preferences'].get('speech') == 'Strict instrumental' and reference.get('provider') != 'jellyfin':
                    raise SessionError('Strict instrumental requires verified library tracks')
                if reference.get('provider') == 'jellyfin':
                    import online_services
                    replay = recipe['music'].pop('_station_replay', None)
                    try:
                        if replay is not None:
                            candidates = replay
                        elif station['generation']['kind'] == 'audiomuse':
                            candidates = online_services.audiomuse_similar(reference, cancel_event=cancel)
                        else:
                            candidates = online_services.jellyfin_queue(reference, cancel_event=cancel)
                    except (OSError, ValueError, hub.HubError):
                        if not valid() or replay is not None or station['preferences'].get('speech') == 'Strict instrumental' or not station['fallbacks']:
                            raise
                        for fallback_offset, fallback in enumerate(station['fallbacks'], 1):
                            try:
                                source = self._source({'music': fallback})
                                generated_fallback = True
                                break
                            except (ValueError, hub.HubError):
                                continue
                        if not generated_fallback:
                            raise
                        candidates = None
                    if not valid():
                        return
                    if candidates is not None:
                        for candidate in candidates[:8]:
                            if self._metadata_needs_refresh(candidate):
                                try:
                                    self._metadata_job('enrich', {'reference': candidate})
                                except (ValueError, ImportError):
                                    break
                        if station['preferences'].get('speech') == 'Likely instrumental':
                            self._schedule_analysis(candidates)
                        new_program = station_engine.Program(station, candidates, trusted_resolver=self._metadata_cached)
                        if replay is None:
                            memory = self.station_memory.get(station['id']) or {}
                            new_program.liked_artists = {x for x in memory.get('liked_artists', []) if isinstance(x, str)}
                            new_program.disliked = {tuple(x) for x in memory.get('disliked', []) if isinstance(x, list) and len(x) == 3 and all(isinstance(y, (str, type(None))) for y in x)}
                            previous = [track for entry in self.station_history
                                        if (entry.get('station') or {}).get('id') == station['id']
                                        and entry.get('time', 0) > memory.get('reset_at', -1)
                                        for track in reversed(entry.get('tracks') or []) if isinstance(track, dict)][:station_engine.MAX_QUEUE]
                            new_program.consumed = {station_engine.identity(x) for x in previous}
                            new_program.retune()
                            while not new_program.queue and previous:
                                new_program.consumed.discard(station_engine.identity(previous.pop()))
                                new_program.retune()
                        if replay is not None:
                            new_program.station['end_policy'] = 'stop'
                            new_program.queue = []
                            new_program.replay = _copy(replay)
                            new_program.refill()
                        item = new_program.next()
                        if not item:
                            raise SessionError('This station has no playable tracks')
                        source = dict(source, reference=item)
                        recipe['music']['reference'] = item
            recipe['music'].pop('_station_continuation', None)
            if station and station['preferences'].get('speech') == 'Strict instrumental':
                import metadata_pipeline
                ref = source.get('reference') or {}
                if ref.get('provider') != 'jellyfin' or ref.get('type') != 'Audio':
                    raise SessionError('Strict instrumental requires reviewed library tracks')
                snapshot = metadata_pipeline.acquire_verified_snapshot(ref, cancel_event=cancel)
                verified_file = snapshot['file']
                digest = snapshot['audio_sha256']
                if not valid():
                    return
                origin = metadata_pipeline.library_origin()
                if snapshot.get('origin') != origin:
                    raise SessionError('Library changed during audio verification')
                with self.lock:
                    if not valid():
                        return
                    record = self.metadata_store.inspect(origin, ref.get('id'), digest)
                    if not record:
                        self._metadata_observe(origin, ref.get('id'), digest)
                    if not record or (record.get('review') or {}).get('classification') != 'instrumental':
                        raise SessionError('Recording has not been reviewed as instrumental or has changed')
                    if len(self.verified_units) >= 20:
                        raise SessionError('Too many pending verified players; restart the room service to recover them')
                    player_unit = 'listening-hub-radio-' + os.urandom(6).hex() + '.service'
                    strict_guard = {'record': (origin, ref['id']), 'digest': digest, 'event': threading.Event(), 'unit': player_unit}
                    self.verified_units.append({'unit': player_unit, 'origin': origin, 'item_id': ref['id'], 'audio_sha256': digest})
                    self._save_verified_units()
                    self.strict_pending = strict_guard
                playback_cancel = _CombinedCancel(cancel, strict_guard['event'])
            attempts = [source, source]
            if station and station['preferences'].get('speech') != 'Strict instrumental':
                for fallback in station.get('fallbacks', [])[fallback_offset if generated_fallback else 0:]:
                    try:
                        attempts.append(self._source({'music': fallback}))
                    except (ValueError, hub.HubError):
                        continue
            used_fallback = generated_fallback
            for index, attempt in enumerate(attempts):
                try:
                    if strict_guard and playback_cancel.is_set():
                        raise SessionError('Instrumental review was revoked')
                    actual = (self.backend.start_verified_music(attempt, playback_cancel, verified_file, strict_guard['unit'])
                              if strict_guard else self.backend.start_music(attempt, cancel))
                    if strict_guard:
                        strict_started = True
                        if playback_cancel.is_set():
                            raise SessionError('Instrumental review was revoked')
                    if index >= 2 or generated_fallback:
                        used_fallback = True
                        new_program = None
                        for field in ('reference', 'queue', 'queue_index', 'collection_reference', 'url'):
                            recipe['music'].pop(field, None)
                    break
                except (OSError, ValueError, hub.HubError):
                    if not valid() or (strict_guard and playback_cancel.is_set()) or index == len(attempts) - 1 or not station:
                        raise
            if not valid():
                return
            for field in ('id', 'url', 'name', 'kind', 'reference', 'queue', 'queue_index'):
                if field in actual:
                    if field in ('queue', 'queue_index') and recipe['music'].get('collection_reference'):
                        continue
                    recipe['music']['source_id' if field == 'id' else field] = actual[field]
            if used_fallback and (actual.get('reference') or {}).get('provider') == 'jellyfin':
                candidates = actual.get('queue') or []
                if candidates:
                    new_program = station_engine.Program(station, candidates, trusted_resolver=self._metadata_cached)
                    first = _copy(candidates[0])
                    new_program.consumed.add(station_engine.identity(first))
                    new_program.recent = [first]
                    new_program.played = [first]
                    new_program.retune()
                    recipe['music']['reference'] = first
                    used_fallback = False
            known_source = any(item['id'] == actual.get('id') for item in hub.sources())
            private_url = bool(actual.get('url') and hub.private_favorite(actual))
            if known_source or actual.get('reference') or private_url:
                recipe['music'].pop('url', None)
            recipe['music'].pop('mpv_options', None)
            if private_url and not known_source and not actual.get('reference'):
                recipe['music']['replay_requires_favorite'] = True
            rescaled = recipe.get('room_volume') is not None and float(recipe['room_volume']) != self.room_volume
            if recipe.get('room_volume') is not None:
                self.room_volume = float(recipe['room_volume'])
            recipe['room_volume'] = self.room_volume
            self.backend.music_control('volume', self._effective(recipe['music']['volume']))
            self.backend.music_control('muted', recipe['music']['muted'])
            if recipe['music'].get('position') and recipe['music'].get('reference'):
                self.backend.music_control('seek', recipe['music']['position'])
            with self.lock:
                if not valid():
                    return
                if strict_guard and strict_guard['event'].is_set():
                    raise SessionError('Instrumental review was revoked')
                obsolete_units = [entry for entry in self.verified_units if not strict_guard or entry['unit'] != strict_guard['unit']]
                if obsolete_units:
                    self.worker.submit(self._recover_verified_players, obsolete_units)
                old_verified, self.verified_audio = self.verified_audio, verified_file
                self.verified_record = strict_guard['record'] if strict_guard else None
                self.verified_digest = strict_guard['digest'] if strict_guard else None
                self.pending_revocation_stop = None
                verified_file = None
                if old_verified:
                    old_verified.close()
                self.active = recipe
                if station and new_program:
                    self.program = new_program
                elif station and continuation and program_next is not None and not used_fallback:
                    self.program = program_next
                elif not station or used_fallback or not continuation:
                    self.program = None
                if station:
                    if not continuation or not self.station_history:
                        self.station_history.insert(0, {'id': hashlib.sha256((station['id'] + str(self.clock()) + str(generation)).encode()).hexdigest()[:16],
                                                        'station': _copy(station), 'time': self.clock(), 'source_index': recipe['music'].get('station_source_index', 0), 'tracks': []})
                        self.station_history = self.station_history[:20]
                    ref = recipe['music'].get('reference')
                    if ref and self.station_history and not reconnecting:
                        tracks = self.station_history[0]['tracks']
                        tracks.append(_copy(ref))
                        del tracks[:-station_engine.MAX_RECENT]
                self.last_session = _copy(recipe)
                self.last_entered_at = self.clock()
                self.recent = [recipe['id']] + [r for r in self.recent if r != recipe['id']][:19]
                self._persist()
            # Picture and atmosphere failures do not roll back working music.
            for layer, launch, selection in (
                    ('atmosphere', self.backend.start_atmosphere, recipe['atmosphere']),
                    ('picture', self.backend.start_picture, recipe['picture'])):
                if not valid():
                    return
                if previous and previous.get(layer) == selection:
                    if layer == 'atmosphere' and rescaled and self.live.get('atmosphere', {}).get('status') == 'Playing':
                        # An unchanged ambience keeps playing, so it needs the new multiplier too.
                        try:
                            self.backend.atmosphere_control('volume', self._effective(selection['volume']))
                        except (OSError, ValueError, hub.HubError):
                            pass
                    continue
                try:
                    if layer == 'atmosphere':
                        actual_layer = launch(self._scaled(selection), cancel)
                        with self.lock:
                            if valid():
                                self._commit_atmosphere(selection, actual_layer)
                                self.last_session = _copy(self.active)
                                self._persist()
                    else:
                        if 'cancel' in inspect.signature(launch).parameters:
                            launch(selection, cancel=cancel)
                        else:
                            launch(selection)
                except (OSError, ValueError, hub.HubError) as error:
                    if valid():
                        with self.lock:
                            self.errors[layer] = str(error)
            if valid():
                self._route_preferred()
                self._home_action('activate_room_scene', recipe.get('room_id'), cancel_event=cancel)
        except (OSError, ValueError, hub.HubError) as error:
            if valid():
                with self.lock:
                    self.errors['music'] = str(error)
        finally:
            if verified_file:
                verified_file.close()
            with self.lock:
                revoked = bool(strict_guard and (strict_guard['event'].is_set() or cancel.is_set()))
                if strict_guard and self.strict_pending is strict_guard:
                    self.strict_pending = None
                if generation == self.generation:
                    self.transition = None
                    self.auto_advance_pending = False
            if revoked and strict_started:
                try:
                    self.backend.music_control('stop')
                except (OSError, ValueError, hub.HubError):
                    pass
            self.refresh()

    def _home_action(self, method, *args, **kwargs):
        try:
            import online_services
            operation = getattr(online_services, method, None)
            if operation:
                operation(*args, **kwargs)
        except (OSError, ValueError, hub.HubError) as error:
            with self.lock:
                self.errors['connections'] = str(error)

    def _capture(self):
        with self.lock:
            if not self.active:
                raise SessionError('Enter a room before saving its mix')
            recipe = _copy(self.active)
            layers = _copy(self.live)
            recipe['audio'] = {key: self.settings.get(key) for key in
                               ('preferred_output', 'pause_on_disconnect', 'duck_on_call')}
            recipe['room_volume'] = self.room_volume
        if recipe['music'].get('replay_requires_favorite'):
            raise SessionError('Save this private stream as a favorite, select it, then save the room')
        for key in ('music', 'atmosphere'):
            live = layers.get(key) or {}
            for field in ('volume', 'muted'):
                if field in live and live[field] is not None:
                    recipe[key][field] = self._level_of(key, live[field]) if field == 'volume' else live[field]
            duck = (self.ducked or {}).get(key)
            if duck and duck['identity'] == self._layer_identity(key):
                recipe[key]['volume'] = self._level_of(key, duck['volume'])
        atmosphere = layers.get('atmosphere') or {}
        if atmosphere.get('status') != 'Stopped':
            for field in ('source_id', 'kind', 'url', 'name', 'window_open'):
                if field in atmosphere:
                    recipe['atmosphere'][field] = atmosphere[field]
        if recipe['music'].get('reference') and self.media.get('seekable'):
            recipe['music']['position'] = self.media.get('position', 0)
        picture = layers.get('picture') or {}
        if picture.get('placement'):
            recipe['picture']['placement'] = picture['placement']
        self._validate_recipe(recipe)
        return recipe

    def save(self, args):
        name = hub.clean(args.get('name'))
        if not name:
            raise SessionError('Name your room variant')
        recipe = self._capture()
        for key in ('music', 'atmosphere', 'picture'):
            if key in args:
                recipe[key].update(args[key])
        self._validate_recipe(recipe)
        recipe.update(id='variant-' + hashlib.sha256(name.encode()).hexdigest()[:16], name=name)
        with self.lock:
            variants = [v for v in self.variants() if v['id'] != recipe['id']]
            hub.save_json(self.directory / 'room-variants.json', variants + [recipe])
            self.active = recipe
            self.last_session = _copy(recipe)
            self._persist()
        self._changed()
        return {'message': 'Saved ' + name, 'variant': _copy(recipe)}

    def rename(self, args):
        name = hub.clean(args.get('name'))
        if not name:
            raise SessionError('Name your room')
        with self.lock:
            variants = self.variants()
            current = next((v for v in variants if v['id'] == args.get('id')), None)
            if current is None:
                raise SessionError('Saved room not found')
            identity = 'variant-' + hashlib.sha256(name.encode()).hexdigest()[:16]
            if identity != current['id'] and any(v['id'] == identity for v in variants):
                raise SessionError('You already have a room with that name')
            renamed = dict(current, id=identity, name=name)
            hub.save_json(self.directory / 'room-variants.json', [renamed if v is current else v for v in variants])
            for held in (self.active, self.last_session):
                if held and held.get('id') == current['id']:
                    held.update(id=identity, name=name)
            self.recent = [identity if r == current['id'] else r for r in self.recent]
            self._persist()
        self._changed()
        return {'message': 'Renamed to ' + name, 'variant': _copy(renamed)}

    def quiet(self, minutes=None):
        duration = float(minutes or 0)
        if not math.isfinite(duration) or not 0 <= duration <= 1440:
            raise SessionError('Choose an interruption from 0 to 1440 minutes')
        self._invalidate()
        with self.lock:
            current = _copy(self.live)
            identity = self.active.get('id') if self.active else None
        paused, errors = [], {}
        for layer, control in (('music', self.backend.music_control),
                               ('atmosphere', self.backend.atmosphere_control),
                               ('picture', self.backend.picture_control)):
            if current.get(layer, {}).get('status') == 'Playing':
                try:
                    control('pause')
                    paused.append(layer)
                except (OSError, ValueError, hub.HubError) as error:
                    errors[layer] = str(error)
        try:
            self.backend.extra_video_control('pause')
        except (OSError, ValueError, hub.HubError) as error:
            errors['video'] = str(error)
        with self.lock:
            self.resume_offer = {'layers': paused, 'session_id': identity, 'ready_at': self.clock() + duration * 60,
                                 'ready': not duration, 'message': 'Resume when you are ready'} if paused else None
            self.errors.update(errors)
            for layer in paused:
                self.live[layer]['status'] = 'Paused'
        self._changed()
        return {'message': 'Room quiet. Resume is always explicit.', 'paused': paused, 'errors': errors}

    def resume(self):
        with self.lock:
            offer = _copy(self.resume_offer)
            identity = self.active.get('id') if self.active else None
        if not offer:
            raise SessionError('There is no paused room to resume')
        if identity != offer['session_id']:
            raise SessionError('The paused room has changed; choose it again to resume')
        for layer in offer['layers']:
            control = {'music': self.backend.music_control, 'atmosphere': self.backend.atmosphere_control,
                       'picture': self.backend.picture_control}[layer]
            control('play')
            self.live[layer]['status'] = 'Playing'
        with self.lock:
            self.resume_offer = None
        self._changed()
        return {'message': 'Room resumed'}

    def stop(self):
        self._invalidate()
        errors = {}
        for layer, control in (('music', self.backend.music_control),
                               ('atmosphere', self.backend.atmosphere_control),
                               ('picture', self.backend.picture_control)):
            try:
                control('stop')
            except (OSError, ValueError, hub.HubError) as error:
                if self.live.get(layer, {}).get('status') != 'Stopped':
                    errors[layer] = str(error)
        try:
            self.backend.extra_video_control('stop')
        except (OSError, ValueError, hub.HubError) as error:
            errors['video'] = str(error)
        with self.lock:
            if self.verified_audio:
                self.verified_audio.close()
                self.verified_audio = None
                self.verified_record = None
                self.verified_digest = None
            if 'music' not in errors and not self.recovery_blocked and self.verified_units:
                self.worker.submit(self._recover_verified_players, list(self.verified_units))
            self.active = None
            self.program = None
            self.resume_offer = None
            self.live = {name: {'status': 'Stopped'} for name in self.live}
            self.errors.update(errors)
        # Restoring optional remote lighting must not hold the urgent Stop RPC
        # behind a network request. The adapter serializes its own snapshot and
        # restore operations against an activation that is still finishing.
        self.worker.submit(self._home_action, 'restore_room_scene')
        self._changed()
        return {'message': 'Room stopped', 'errors': errors}

    def leave(self, seconds=1.2):
        seconds = float(seconds)
        if not math.isfinite(seconds) or not 0 <= seconds <= 30:
            raise SessionError('Choose a fade from 0 to 30 seconds')
        if not seconds:
            return self.stop()
        return self._fade(seconds, leave=True)

    def _fade(self, seconds, leave=False):
        generation, cancel = self._invalidate()
        started = time.monotonic()
        volumes = {layer: self.live.get(layer, {}).get('volume', 0) or 0
                   for layer in ('music', 'atmosphere')}
        self.transition = {'kind': 'leave' if leave else 'wind_down', 'seconds': seconds, 'id': generation}
        if not leave:
            self.timer_ends['wind_down'] = self.clock() + seconds
        def tick():
            if self.closed or cancel.is_set() or generation != self.generation:
                return
            progress = min(1, (time.monotonic() - started) / seconds)
            for layer in ('music', 'atmosphere') if leave else ('music',):
                if self.live.get(layer, {}).get('status') == 'Playing':
                    try:
                        control = self.backend.music_control if layer == 'music' else self.backend.atmosphere_control
                        control('volume', volumes[layer] * (1 - progress))
                    except (OSError, ValueError, hub.HubError) as error:
                        self.errors[layer] = str(error)
            if progress >= 1:
                if leave:
                    self.stop()
                else:
                    try:
                        self.backend.music_control('stop')
                    except (OSError, ValueError, hub.HubError) as error:
                        self.errors['music'] = str(error)
                    self.transition = None
                    self.timer_ends.pop('wind_down', None)
                    self.live['music']['status'] = 'Stopped'
                self._changed()
                return
            self.timer = threading.Timer(min(.2 if leave else 2, seconds / 10), tick)
            self.timer.daemon = True
            self.timer.start()
        tick()
        self._changed()
        return {'message': 'Leaving room' if leave else 'Music will fade while atmosphere continues'}

    def layer(self, args):
        name, action, value = args.get('layer'), args.get('action'), args.get('value')
        if name not in ('music', 'atmosphere', 'picture'):
            raise SessionError('Choose Music, Atmosphere or Picture')
        if action in ('stop', 'pause'):
            self._invalidate()
        if self.ducked is not None and name in ('music', 'atmosphere') and action in ('volume', 'mute', 'muted'):
            # An intentional adjustment during a call belongs to the user. A
            # later call-end event must not overwrite it with an old level.
            self.ducked.pop(name, None)
            self.duck_manual.add(name)
        generation = args.get('_request_generation', self.generation)
        cancel = args.get('_cancel_event', self.cancel)
        if generation != self.generation or cancel.is_set():
            return {'message': 'Room update cancelled'}
        if action == 'place':
            action, value = 'placement', args.get('placement')
        if action == 'openness':
            action = 'window'
        if action == 'sequence' and name == 'picture':
            return self.sequence(args.get('minutes', 30))
        if action in ('next', 'previous') and name == 'music':
            return self.queue_step(1 if action == 'next' else -1)
        if action == 'next' and name == 'atmosphere':
            return self._atmosphere_next()
        if action in ('select', 'retry'):
            if name == 'atmosphere':
                selection = {key: args[key] for key in ('kind', 'source_id', 'url', 'name', 'volume', 'muted', 'window_open', 'reference', 'resolver') if key in args}
                if action == 'retry' and self.active:
                    selection = _copy(self.active['atmosphere'])
                elif self.active:
                    for field in ('volume', 'muted'):
                        selection.setdefault(field, self.active['atmosphere'].get(field, 20 if field == 'volume' else False))
                selection.setdefault('kind', 'radio' if selection.get('reference') or selection.get('url') else 'local')
                with self.lock:
                    self.atmosphere_generation += 1
                    layer_generation = self.atmosphere_generation
                    self.atmosphere_eof_attempted = None
                    self.atmosphere_pending = False
                actual_layer = self.backend.start_atmosphere(self._scaled(selection), cancel)
                self._commit_atmosphere(selection, actual_layer)
                if self.active and generation == self.generation and not cancel.is_set():
                    with self.lock:
                        if layer_generation == self.atmosphere_generation:
                            self.active['atmosphere'] = selection
            elif name == 'picture':
                if not self.active:
                    raise SessionError('Enter a room first')
                picture = _copy(self.active['picture'])
                if 'enabled' not in args:
                    picture['enabled'] = True
                picture.update({key: args[key] for key in ('preset_id', 'enabled', 'still', 'placement', 'display') if key in args})
                self._start_picture(picture, cancel)
                if generation == self.generation and not cancel.is_set():
                    self.active['picture'] = picture
            else:
                if not self.active:
                    raise SessionError('Choose a room first')
                return self.enter({'recipe': self._capture()})
        elif name == 'picture' and action in ('still', 'motion'):
            if not self.active:
                raise SessionError('Enter a room first')
            picture = _copy(self.active['picture'])
            picture['still'] = bool(args.get('still', True)) if action == 'still' else False
            self._start_picture(picture, cancel)
            if generation == self.generation and not cancel.is_set():
                self.active['picture'] = picture
        else:
            operation = {'music': self.backend.music_control, 'atmosphere': self.backend.atmosphere_control,
                         'picture': self.backend.picture_control}[name]
            operation(action, self._effective(level(value)) if action == 'volume' and self.room_volume < 100 else value)
            if self.active:
                field = {'window': 'window_open', 'placement': 'placement', 'volume': 'volume'}.get(action)
                if field:
                    self.active[name][field] = value
                if action == 'stop':
                    if name == 'music':
                        self.live.setdefault('music', {})['status'] = 'Stopped'
                        self.media = {}
                        self.auto_advance_pending = False
                    elif name == 'atmosphere':
                        self.active[name]['kind'] = 'none'
                    elif name == 'picture':
                        self.active[name]['enabled'] = False
        with self.lock:
            if generation != self.generation or cancel.is_set():
                return {'message': 'Room update cancelled'}
            self.errors.pop(name, None)
            if self.active:
                self.last_session = _copy(self.active)
                self._persist()
        self.refresh()
        if name == 'atmosphere' and action in ('select', 'retry'):
            self._route_preferred()
        return {'message': name.capitalize() + ' updated'}

    def _start_picture(self, picture, cancel):
        if 'cancel' in inspect.signature(self.backend.start_picture).parameters:
            return self.backend.start_picture(picture, cancel=cancel)
        return self.backend.start_picture(picture)

    @staticmethod
    def _commit_atmosphere(selection, actual):
        if not isinstance(actual, dict) or not actual.get('reference'):
            return
        selection['source_id'] = actual.get('id', selection.get('source_id'))
        if not selection.get('collection_reference'):
            queue = actual.get('queue') or []
            selection['queue'] = _copy(queue)
            selection['queue_index'] = 0
            if queue:
                selection['collection_reference'] = _copy(actual['reference'])
                selection['reference'] = _copy(queue[0])

    def _atmosphere_next(self, automatic=False):
        with self.lock:
            if not self.active or self.atmosphere_pending or self.transition:
                raise SessionError('There is no next background track')
            selection = _copy(self.active['atmosphere'])
            queue = selection.get('queue') or []
            index = selection.get('queue_index', 0) + 1
            if not 0 <= index < len(queue):
                if automatic:
                    self.errors['atmosphere'] = 'Background queue exhausted'
                    return {'message': 'Background queue exhausted'}
                raise SessionError('There is no next background track')
            selection.update(reference=queue[index], queue_index=index)
            expected, layer_generation, cancel = self.generation, self.atmosphere_generation, self.cancel
            self.atmosphere_pending = True

        def advance():
            try:
                actual = self.backend.start_atmosphere(self._scaled(selection), cancel)
                self._commit_atmosphere(selection, actual)
                with self.lock:
                    if (not cancel.is_set() and expected == self.generation
                            and layer_generation == self.atmosphere_generation and self.active):
                        self.active['atmosphere'] = selection
                        self.last_session = _copy(self.active)
                        self.errors.pop('atmosphere', None)
                        self._persist()
            except (OSError, ValueError, hub.HubError) as error:
                with self.lock:
                    if not cancel.is_set() and expected == self.generation and layer_generation == self.atmosphere_generation:
                        self.errors['atmosphere'] = str(error)
            finally:
                with self.lock:
                    if expected == self.generation and layer_generation == self.atmosphere_generation:
                        self.atmosphere_pending = False
                self._changed()
        self.worker.submit(advance)
        return {'accepted': True, 'message': 'Changing background track'}

    def queue_step(self, step, automatic=False):
        with self.lock:
            expected_generation = self.generation
            if not self.active:
                raise SessionError('Choose a library album or playlist first')
            recipe = self._capture()
            music = recipe['music']
            if self.program and music.get('station') and step > 0:
                next_program = _copy(self.program)
                item = next_program.next()
                if not item:
                    self.errors['music'] = 'Station queue exhausted'
                    self.auto_advance_pending = False
                    return {'message': 'Station queue exhausted'}
                music.update(reference=item, position=0, _station_continuation=True,
                             station=_copy(next_program.station))
                music.pop('collection_reference', None)
                return self.enter({'recipe': recipe, '_automatic': automatic, '_request_generation': expected_generation,
                                   '_program_next': next_program})
            queue = music.get('queue') or []
            index = int(music.get('queue_index', 0)) + step
            if not 0 <= index < len(queue):
                raise SessionError('There is no next track' if step > 0 else 'There is no previous track')
            parent_reference = music.get('collection_reference') or music.get('reference')
            music.update(reference=queue[index], queue_index=index, position=0)
            music['collection_reference'] = parent_reference
            # Preparing a single track yields a one-item queue. Preserve the public
            # collection queue explicitly for subsequent Next/Previous operations.
            result = self.enter({'recipe': recipe, '_automatic': automatic, '_request_generation': expected_generation})
            return result

    def _route_preferred(self):
        if not self.settings.get('preferred_output'):
            return
        try:
            import audio_devices
            audio_devices.route(self.settings['preferred_output'], remember=False)
        except (ImportError, OSError, ValueError) as error:
            self.errors['output'] = str(error)

    def sequence(self, minutes=30):
        duration = float(minutes)
        if not math.isfinite(duration) or not 0 <= duration <= 1440:
            raise SessionError('Choose a lighting sequence from 0 to 1440 minutes')
        self.lighting_generation += 1
        identity = self.lighting_generation
        if self.lighting_timer:
            self.lighting_timer.cancel()
            self.lighting_timer = None
        self.timer_ends.pop('dim', None)
        if not duration:
            ambience.set_lighting(0.)
            return {'message': 'Original room lighting restored'}
        if self.settings.get('reduced_motion'):
            raise SessionError('Turn off reduced motion before starting changing room lighting')
        started = time.monotonic()
        self.timer_ends['dim'] = self.clock() + duration * 60
        def tick():
            if self.closed or self.lighting_generation != identity:
                return
            progress = min(1., (time.monotonic() - started) / (duration * 60))
            if progress >= 1:
                self.timer_ends.pop('dim', None)
            try:
                ambience.set_lighting(progress)
            except (OSError, ValueError) as error:
                self.errors['picture'] = str(error)
                return
            if progress < 1:
                self.lighting_timer = threading.Timer(5, tick)
                self.lighting_timer.daemon = True
                self.lighting_timer.start()
        tick()
        return {'message': 'Room lighting will slowly move from dusk to night'}

    def apply_settings(self, values):
        allowed = {'placement', 'still', 'reduced_motion', 'preferred_output', 'pause_on_disconnect',
                   'duck_on_call', 'preferred_display', 'display', 'lighting_sequence', 'keep_controls'}
        if not isinstance(values, dict) or set(values) - allowed:
            raise SessionError('Unknown room setting')
        if values.get('placement', self.settings['placement']) not in ('9', 'here', 'pin', 'pip'):
            raise SessionError('Choose a supported picture location')
        routing = {key: values[key] for key in ('preferred_output', 'pause_on_disconnect', 'duck_on_call') if key in values}
        adapter, prior_routing = None, None
        try:
            import audio_devices
            adapter = audio_devices
        except ImportError:
            pass
        with self.lock:
            previous = _copy(self.settings)
            if routing and adapter is not None:
                prior_routing = adapter.settings()
                # The routing file is atomic. If it cannot be written, neither
                # the session's settings nor its durable return offer changes.
                adapter.configure(**routing)
            self.settings = dict(previous, **values)
            try:
                self._persist()
            except (OSError, ValueError):
                self.settings = previous
                if prior_routing is not None:
                    try:
                        adapter.configure(**prior_routing)
                    except (OSError, ValueError) as rollback_error:
                        self.errors['output'] = 'Could not restore audio preferences: ' + str(rollback_error)
                raise
        if 'preferred_output' in values and values['preferred_output']:
            self._route_preferred()
        self._changed()
        return {'message': 'Room preferences saved'}

    def poll_policy(self):
        try:
            import audio_devices
            state = audio_devices.poll_policy(detect_calls=bool(self.settings.get('duck_on_call')))
        except (ImportError, OSError, ValueError) as error:
            self.policy_status = {'error': str(error)}
            return
        self.policy_status = state
        if state.get('disconnect') and (self.transition or any(
                self.live.get(name, {}).get('status') == 'Playing' for name in ('music', 'atmosphere'))):
            self.quiet()
        if state.get('duck') or (state.get('call_active') and self.settings.get('duck_on_call')):
            if self.ducked is None:
                self.ducked = {}
                self.duck_manual.clear()
            for name in ('music', 'atmosphere'):
                live = self.live.get(name) or {}
                identity = self._layer_identity(name)
                existing = self.ducked.get(name)
                if existing and existing['identity'] == identity:
                    continue
                if name in self.duck_manual:
                    continue
                if live.get('status') == 'Playing' and live.get('volume') is not None:
                    self.ducked[name] = {'volume': live['volume'], 'identity': identity}
                    control = self.backend.music_control if name == 'music' else self.backend.atmosphere_control
                    control('volume', live['volume'] * .2)
        elif self.ducked is not None:
            for name, saved in self.ducked.items():
                if saved['identity'] == self._layer_identity(name):
                    control = self.backend.music_control if name == 'music' else self.backend.atmosphere_control
                    control('volume', saved['volume'])
            self.ducked = None
            self.duck_manual.clear()

    def _layer_identity(self, name):
        selection = (self.active or {}).get(name) or {}
        return (selection.get('source_id') or selection.get('id'), selection.get('url'),
                repr(selection.get('reference')))

    def dispatch(self, action, args=None):
        args = args or {}
        if not isinstance(args, dict):
            raise SessionError('Session arguments must be an object')
        if action in ('inspect', 'get'):
            return self.snapshot()
        if action.startswith('metadata.'):
            if action == 'metadata.status':
                import metadata_pipeline
                return {'acoustid_ready': bool(metadata_pipeline.acoustid_client_key())}
            if action == 'metadata.configure_acoustid':
                import metadata_pipeline
                result = metadata_pipeline.configure_acoustid_client_key(args.get('client_key'))
                return {'message': 'AcoustID application key saved' if result['configured'] else 'AcoustID application key cleared', 'acoustid_ready': result['configured']}
            if action == 'metadata.list':
                page = self.metadata_store.export_page(limit=100)
                return {'records': page['records'], 'truncated': page['next_cursor'] is not None}
            if action == 'metadata.export':
                return self.metadata_store.export_page(cursor=args.get('cursor'), limit=args.get('limit', 50))
            if action == 'metadata.import':
                return {'imported': self.metadata_store.import_records(args.get('records'))}
            if action == 'metadata.inspect':
                return {'record': self.metadata_store.current(args.get('origin'), args.get('item_id'))}
            if action == 'metadata.lookup_reference':
                return {'record': self._metadata_cached(args.get('reference'))}
            if action == 'metadata.jobs':
                with self.lock:
                    return {'jobs': [{k: v for k, v in job.items() if not k.startswith('_')} for job in self.metadata_jobs.values()]}
            if action == 'metadata.cancel':
                with self.lock:
                    job = self.metadata_jobs.get(args.get('job_id'))
                    if not job:
                        raise SessionError('Metadata job not found')
                    job['_cancel'].set()
                return {'message': 'Cancellation requested'}
            if action == 'metadata.confirm_identity':
                import metadata_pipeline
                with self.lock:
                    job = self.metadata_jobs.get(args.get('job_id'))
                    candidates = (job.get('result') or {}).get('candidates', []) if job and job['kind'] == 'match' and job['status'] == 'completed' else []
                    selected = next ( (c for c in candidates if c['provider_id'] == args.get('provider_id')), None)
                    if not selected or not job.get('_record') or job['_record'][0] != metadata_pipeline.library_origin():
                        raise SessionError('Choose a candidate from the current library')
                    origin, item_id = job['_record']
                    record = self.metadata_store.current(origin, item_id)
                    if not record:
                        raise SessionError('Recording no longer available')
                    import json
                    revision = job.get('_revision')
                    revision_hash = hashlib.sha256(json.dumps(revision, sort_keys=True, separators=(',', ':')).encode()).hexdigest() if revision is not None else None
                    if not revision_hash or not any(x.get('name') == 'revision' and x.get('value') == revision_hash for x in record['identities']):
                        raise SessionError('Recording changed; search again')
                    identity = {'provider': selected['provider'], 'provider_id': selected['provider_id'], 'name': 'recording', 'method': 'user selected candidate', 'status': 'confirmed'}
                    identities = [x for x in record['identities'] if not (x.get('provider') == 'MusicBrainz' and x.get('name') == 'recording')] + [identity]
                    self._metadata_observe(origin, item_id, record['audio_sha256'], identities, record['features'])
                return {'message': 'Recording identity confirmed'}
            if action in ('metadata.match', 'metadata.analyze'):
                return self._metadata_job(action.split('.', 1)[1], args)
            if action == 'metadata.enrich':
                return self._metadata_job('enrich', args)
            if action == 'metadata.review':
                if args.get('acknowledged') is not True:
                    raise SessionError('Confirm that you reviewed the whole recording')
                if args.get('classification') not in ('instrumental', 'vocal', 'spoken', 'unknown'):
                    raise SessionError('Choose a review classification')
                return self._metadata_job('review', args)
            if action == 'metadata.revoke':
                with self.lock:
                    for job in self.metadata_jobs.values():
                        if job.get('_record') == (args.get('origin'), args.get('item_id')) and job['status'] in ('queued', 'running'):
                            job['_cancel'].set()
                    record = self.metadata_store.current(args.get('origin'), args.get('item_id'))
                    if not record or not record['audio_sha256']:
                        raise SessionError('Recording not found')
                    self.metadata_store.revoke(record['origin'], record['item_id'], record['audio_sha256'])
                    self._check_pending_review()
                return {'message': 'Review revoked'}
            raise SessionError('Unknown metadata action')
        if action.startswith('station.'):
            with self.lock:
                if action == 'station.list':
                    return {'stations': self.stations()}
                if action == 'station.recent':
                    return {'recent': _copy(self.station_history)}
                if action in ('station.save', 'station.update'):
                    if self.transition:
                        raise SessionError('Wait until the current room change finishes before saving a station')
                    existing = next((s for s in self.stations() if s['id'] == args.get('id')), None)
                    if action == 'station.update':
                        if not existing:
                            raise SessionError('Station not found')
                        value = dict(existing, preferences=dict(existing['preferences'], **(args.get('preferences') or {})))
                    else:
                        value = args.get('station')
                    station = station_engine.normalize(value)
                    if (self.active and self.active.get('music', {}).get('station', {}).get('id') == station['id']
                            and station['preferences'].get('speech') == 'Strict instrumental'):
                        raise SessionError('Strict instrumental cannot be enabled while an unverified source is playing; stop it first')
                    with self.lock:
                        hub.save_json(self.directory / 'stations.json', [s for s in self.stations() if s['id'] != station['id']] + [station])
                        if self.program and self.program.station['id'] == station['id']:
                            self.program.station = _copy(station)
                            self.program.retune()
                            if station['preferences'].get('speech') == 'Likely instrumental':
                                self._schedule_analysis(self.program.candidates)
                            if self.active:
                                self.active['music']['station'] = _copy(station)
                                self.last_session = _copy(self.active)
                                self._persist()
                    return {'message': 'Station saved', 'station': station}
                if action == 'station.play_next':
                    if not self.program or not self.active or self.transition:
                        raise SessionError('Start a library station first')
                    self.program.play_next(args.get('item'))
                    return {'message': 'Added to play next'}
                if action == 'station.feedback':
                    if not self.program or not self.active or self.transition:
                        raise SessionError('Start a library station first')
                    self.program.feedback(args.get('rating'))
                    saved_memory = self.station_memory.get(self.program.station['id']) or {}
                    self.station_memory[self.program.station['id']] = {'liked_artists': sorted(self.program.liked_artists)[-100:],
                                                                       'disliked': [list(x) for x in list(self.program.disliked)[-100:]],
                                                                       'reset_at': saved_memory.get('reset_at', -1)}
                    self._persist()
                    return {'message': 'Upcoming selections updated'}
                if action == 'station.reset_memory':
                    identity = args.get('id')
                    if self.transition:
                        raise SessionError('Wait until the current room change finishes')
                    if not any(s['id'] == identity for s in self.stations()):
                        raise SessionError('Station not found')
                    self.station_memory[identity] = {'reset_at': self.clock()}
                    if self.program and self.program.station['id'] == identity:
                        self.program.liked_artists.clear()
                        self.program.disliked.clear()
                        self.program.retune()
                    self._persist()
                    return {'message': 'Station memory reset'}
                if action == 'station.delete':
                    identity = args.get('id')
                    if self.transition or (self.active and self.active.get('music', {}).get('station', {}).get('id') == identity):
                        raise SessionError('Stop or leave this station before removing it')
                    values = self.stations()
                    if not any(s['id'] == identity for s in values):
                        raise SessionError('Station not found')
                    hub.save_json(self.directory / 'stations.json', [s for s in values if s['id'] != identity])
                    self.station_memory.pop(identity, None)
                    self._persist()
                    return {'message': 'Station removed'}
                if action == 'station.hold':
                    if not self.program or not self.active or self.transition or not isinstance(args.get('hold'), bool):
                        raise SessionError('Choose whether to hold the current mood')
                    self.program.hold = args['hold']
                    return {'message': 'Mood held' if self.program.hold else 'Program arc resumed'}
                if action in ('station.start', 'station.replay'):
                    history = next((s for s in self.station_history if s['id'] == args.get('id')), None) if action == 'station.replay' else None
                    station = history['station'] if history else next((s for s in self.stations() if s['id'] == args.get('id')), None)
                    if not station:
                        raise SessionError('Station not found')
                    if action == 'station.replay' and (not history or not history.get('tracks')):
                        raise SessionError('This listening history has no recorded tracks to replay')
                    previous = next((entry for entry in self.station_history if (entry.get('station') or {}).get('id') == station['id']), None)
                    previous_index = (previous or {}).get('source_index', -1)
                    if not isinstance(previous_index, int) or isinstance(previous_index, bool):
                        previous_index = -1
                    source_index = (history.get('source_index', 0) if history else (previous_index + 1) % len(station['sources']))
                    if not isinstance(source_index, int) or not 0 <= source_index < len(station['sources']):
                        source_index = 0
                    source = _copy(station['sources'][source_index])
                    if history:
                        source = {'reference': _copy(history['tracks'][0])}
                    source['station'] = station
                    source['station_source_index'] = source_index
                    if history:
                        source['_station_replay'] = _copy(history['tracks'])
                    source['source_id'] = source.get('source_id') or source.get('id') or 'library'
                    if self.active:
                        source.setdefault('volume', self.active['music'].get('volume', 50))
                        source.setdefault('muted', self.active['music'].get('muted', False))
                    return self.enter({'music': source})
        if action == 'enter':
            return self.enter(args)
        if action == 'return':
            if not self.last_session:
                raise SessionError('There is no previous room to return to')
            return self.enter({'recipe': _copy(self.last_session)})
        if action == 'save':
            return self.save(args)
        if action == 'variants':
            return {'variants': self.variants()}
        if action == 'rename':
            return self.rename(args)
        if action == 'delete':
            hub.save_json(self.directory / 'room-variants.json', [v for v in self.variants() if v['id'] != args.get('id')])
            return {'message': 'Saved room removed'}
        if action == 'room_volume':
            return self.set_room_volume(args.get('value'), args.get('delta'))
        if action == 'quiet':
            return self.quiet(args.get('minutes'))
        if action == 'resume':
            return self.resume()
        if action == 'stop':
            return self.stop()
        if action == 'leave':
            return self.leave(args.get('fade_seconds', 1.2))
        if action == 'wind_down':
            minutes = float(args.get('minutes', 30))
            if not math.isfinite(minutes) or not 0 <= minutes <= 1440:
                raise SessionError('Choose a wind down from 1 to 1440 minutes')
            if not minutes:
                self._invalidate()
                if self.active:
                    self.backend.music_control('volume', self._effective(self.active['music'].get('volume', 50)))
                return {'message': 'Wind down cancelled; music level restored'}
            return self._fade(minutes * 60)
        if action in ('layer', 'picture'):
            if action == 'picture':
                args = dict(args, layer='picture')
            return self.layer(args)
        if action == 'settings':
            return self.apply_settings(args.get('values') or {})
        if action == 'online':
            reference = args.get('item')
            if not isinstance(reference, dict):
                raise SessionError('Choose a library item')
            if reference.get('provider') == 'link':
                return self.open_link({'url': reference.get('url'), 'destination': reference.get('route', 'music')})
            if reference.get('provider') == 'music_assistant' and reference.get('action') == 'transfer':
                import online_services
                online_services.ma_transfer(reference.get('source_player_id'), reference.get('player_id'))
                if self.active and self.active['music'].get('reference', {}).get('provider') == 'music_assistant':
                    self.active['music']['reference']['player_id'] = reference['player_id']
                    if getattr(self.backend, 'remote', None):
                        self.backend.remote['player_id'] = reference['player_id']
                return {'message': 'Music queue transferred; local atmosphere and picture stay here'}
            return self.enter({'music': {'reference': reference, 'name': reference.get('name', 'Library music'),
                                         'source_id': 'library', 'volume': 50, 'muted': False}})
        if action == 'music':
            item = args.get('item') or {}
            music = dict(item, source_id=item.get('id') or args.get('source_id'))
            if self.active:
                music.setdefault('volume', self.active['music'].get('volume', 50))
                music.setdefault('muted', self.active['music'].get('muted', False))
            options = {'music': music}
            if '_request_generation' in args:
                options['_request_generation'] = args['_request_generation']
            return self.enter(options)
        if action == 'atmosphere':
            return self.layer(dict(args, layer='atmosphere', action='select',
                                   source_id=args.get('source_id') or args.get('id')))
        if action == 'link':
            return self.open_link(args)
        raise SessionError('Unknown listening session action: ' + str(action))

    def close(self):
        if self.verified_audio:
            self.verified_audio.close()
            self.verified_audio = None
        self.closed = True
        self._invalidate()
        if self.lighting_timer:
            self.lighting_timer.cancel()
        self.worker.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            for job in self.metadata_jobs.values():
                job['_cancel'].set()
        self.metadata_worker.shutdown(wait=False, cancel_futures=True)
        self.analysis_worker.shutdown(wait=False, cancel_futures=True)
        try:
            import online_services
            online_services.close_playback_relay()
        except (ImportError, AttributeError):
            pass

    def open_link(self, args):
        url = args.get('url')
        destination = args.get('destination', 'music')
        import online_services
        descriptor = online_services.preview_link(url)
        if destination not in descriptor['actions']:
            raise SessionError('This link cannot be sent to ' + destination + '; open its original service')
        url = descriptor['url']
        generation = args.get('_request_generation', self.generation)
        cancel = args.get('_cancel_event', self.cancel)
        if generation != self.generation or cancel.is_set():
            return {'message': 'Link action cancelled'}
        if destination == 'picture':
            picture = {'preset_id': 'pasted-link', 'url': url, 'enabled': True,
                       'still': False, 'placement': self.settings['placement'], 'display': self.settings.get('display')}
            self._start_picture(picture, cancel)
            if self.active and generation == self.generation and not cancel.is_set():
                self.active['picture'] = picture
                self.last_session = _copy(self.active)
                self._persist()
            return {'message': 'Silent picture opened'}
        if destination == 'atmosphere':
            import urllib.parse
            host = (urllib.parse.urlsplit(url).hostname or '').lower()
            resolver = host in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'm.youtube.com', 'youtu.be', 'twitch.tv', 'www.twitch.tv')
            return self.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'radio', 'url': url, 'resolver': resolver,
                               '_request_generation': generation, '_cancel_event': cancel})
        if destination != 'music':
            raise SessionError('Send this link to Music or Picture')
        import urllib.parse
        host = (urllib.parse.urlsplit(url).hostname or '').lower()
        if host in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'm.youtube.com', 'youtu.be'):
            url = hub.youtube_video_url(url)
            item = {'id': 'youtube-video', 'name': 'YouTube audio', 'url': url, 'resolver': True}
        elif host in ('twitch.tv', 'www.twitch.tv'):
            url = audio_layers._url(url)
            item = {'id': 'twitch-channel', 'name': 'Twitch · ' + url.rstrip('/').rsplit('/', 1)[-1], 'url': url, 'resolver': True}
        else:
            item = {'id': 'custom-stream', 'name': 'Your stream', 'url': url}
        return self.dispatch('music', {'item': item, '_request_generation': generation})
