"""Station queue and intent tests; no player, account or real home is used."""
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import wave
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import station_engine
import room_session
from test_listening_session import FakeBackend


def track(n):
    return {'provider': 'jellyfin', 'id': str(n), 'type': 'Audio', 'name': 'Track ' + str(n)}


def station():
    return {'name': 'Evening', 'sources': [{'source_id': 'library', 'reference': {
        'provider': 'jellyfin', 'id': 'album', 'type': 'MusicAlbum', 'name': 'Album'}}],
        'end_policy': 'stop'}


class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'XDG_CONFIG_HOME': self.temp.name + '/config',
                              'XDG_STATE_HOME': self.temp.name + '/state', 'XDG_CACHE_HOME': self.temp.name + '/cache',
                              'XDG_RUNTIME_DIR': self.temp.name + '/runtime'})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def wav(seconds=.05):
        output = io.BytesIO()
        with wave.open(output, 'wb') as value:
            value.setparams((1, 2, 8000, int(8000 * seconds), 'NONE', ''))
            value.writeframes(b'\0\0' * int(8000 * seconds))
        return output.getvalue()

    def presenter_station(self, **extra):
        value = station()
        value['presenter'] = {'enabled': True, 'budget': {'every_tracks': 2, 'max_seconds': 5}}
        clips = extra.pop('presenter_clips', None)
        value.update(extra)
        if clips is not None:
            value.setdefault('preferences', {})['presenter_clips'] = clips
        return value

    def start_presenter_station(self, controller, definition=None):
        saved = controller.dispatch('station.save', {'station': definition or self.presenter_station()})['station']
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
                mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3), track(4)]):
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            controller.presenter_worker.submit(lambda: None).result(timeout=3)
        return saved

    def test_presenter_plays_actual_duration_muted_without_advancing_music_early(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        segment = {'text': 'Link', 'sources': [{'record': '1'}], 'outcome': 'played',
                   'duration': .15, '_max_seconds': 5, '_audio_path': '/fixture.wav'}
        with mock.patch.object(controller.presenter, 'prepare_slot', return_value=(segment, None)):
            self.start_presenter_station(controller)
            first = controller.active['music']['reference']['id']
            controller.presenter.muted = True
            backend.media = {'eof_reached': True, 'position': 42}
            controller.refresh()
            self.assertTrue(backend.presenter_started.wait(1))
            self.assertEqual(controller.active['music']['reference']['id'], first)
            self.assertIn(('start_presenter', .15, 50.0, True), backend.calls)
            self.assertEqual(controller.snapshot()['layers']['music']['position'], 42)
            controller.presenter_play_worker.submit(lambda: None).result(timeout=2)
            controller.worker.submit(lambda: None).result(timeout=2)
            self.assertNotEqual(controller.active['music']['reference']['id'], first)
            controller.presenter_worker.submit(lambda: None).result(timeout=2)

    def test_station_change_stops_generation_bound_presenter_immediately(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        segment = {'text': 'Long link', 'sources': [], 'outcome': 'played', 'duration': 2,
                   '_max_seconds': 5, '_audio_path': '/fixture.wav'}
        with mock.patch.object(controller.presenter, 'prepare_slot', return_value=(segment, None)):
            self.start_presenter_station(controller)
            backend.media = {'eof_reached': True}
            controller.refresh()
            self.assertTrue(backend.presenter_started.wait(1))
            controller.dispatch('music', {'item': {'id': 'other', 'name': 'Other', 'url': 'https://example.test/music'}})
            self.assertTrue(backend.presenter_stopped.wait(.5))

    def test_prepare_failure_is_the_only_drop_at_verified_track_end(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        record = {'origin': 'library', 'item_id': '1', 'identities': [
            {'provider': 'Jellyfin', 'name': 'title', 'value': 'Track one'}]}
        with mock.patch.object(controller, '_metadata_cached', return_value=record), \
                mock.patch.object(controller.presenter, '_chat', side_effect=ValueError('model missing')):
            self.start_presenter_station(controller)
        backend.media = {'eof_reached': True}
        controller.refresh()
        controller.worker.submit(lambda: None).result(timeout=2)
        snapshot = controller.snapshot()['presenter']
        self.assertIn('model missing', snapshot['status'])
        reasons = [x['reason'] for x in snapshot['segments']]
        self.assertEqual(reasons, ['language unavailable'])
        self.assertFalse(backend.presenter_started.is_set())

    def test_pending_preparation_records_only_late_for_its_slot(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        preparing = threading.Event()
        release = threading.Event()

        def slow_failure(*_args, **_kwargs):
            preparing.set()
            release.wait(2)
            return None, ('language unavailable', ())

        with mock.patch.object(controller.presenter, 'prepare_slot', side_effect=slow_failure):
            saved = controller.dispatch('station.save', {'station': self.presenter_station()})['station']
            with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
                    mock.patch('online_services.jellyfin_queue', return_value=[track(1)]):
                controller.dispatch('station.start', {'id': saved['id']})
                self.assertTrue(preparing.wait(1))
                backend.media = {'eof_reached': True}
                controller.refresh()
                controller.worker.submit(lambda: None).result(timeout=2)
                self.assertEqual(controller.presenter.segments[-1]['reason'], 'late')
                release.set()
                controller.presenter_worker.submit(lambda: None).result(timeout=3)

        reasons = [item['reason'] for item in controller.snapshot()['presenter']['segments']]
        self.assertEqual(reasons, ['late'])

    def test_late_slot_does_not_block_the_next_tracks_preparation(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        release = threading.Event()
        calls = []

        def prepare(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                release.wait(2)
            return None, ('no captured metadata', ())

        station = self.presenter_station()
        station['presenter']['budget']['every_tracks'] = 2
        with mock.patch.object(controller.presenter, 'prepare_slot', side_effect=prepare):
            saved = controller.dispatch('station.save', {'station': station})['station']
            with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
                    mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3), track(4)]):
                controller.dispatch('station.start', {'id': saved['id']})
                controller.worker.submit(lambda: None).result(timeout=3)
                backend.media = {'eof_reached': True}
                controller.refresh()          # slot 1 ends while preparation is still running: late
                controller.worker.submit(lambda: None).result(timeout=3)
                release.set()
                controller.presenter_worker.submit(lambda: None).result(timeout=3)
                self.assertEqual([x['reason'] for x in controller.snapshot()['presenter']['segments']], ['late'])
                backend.media = {'eof_reached': True}
                controller.refresh()          # track 2 is not a slot: nothing is recorded
                controller.worker.submit(lambda: None).result(timeout=3)
                backend.media = {}
                controller.refresh()
                backend.media = {'eof_reached': True}
                controller.refresh()          # track 3 is the next slot and was prepared for
                controller.worker.submit(lambda: None).result(timeout=3)
        self.assertGreaterEqual(len(calls), 2)

    def test_snapshot_ready_tracks_prepared_segment(self):
        controller = room_session.Controller(FakeBackend(), self.temp.name)
        self.addCleanup(controller.close)
        self.assertFalse(controller.snapshot()['presenter']['ready'])
        controller.presenter_slot = {'state': 'ready', 'segment': {'text': 'Ready'}}
        self.assertTrue(controller.snapshot()['presenter']['ready'])

    def test_clip_uses_same_slot_and_primary_speech_suppresses_generation(self):
        clip_path = Path(self.temp.name) / 'clip.wav'
        clip_path.write_bytes(self.wav())
        clip = {'source': 'Local ident', 'audio': str(clip_path),
                'allowed_placement': 'between_tracks', 'expires_at': 9999999999}
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        self.start_presenter_station(controller, self.presenter_station(presenter_clips=[clip]))
        backend.media = {'eof_reached': True}
        controller.refresh()
        self.assertTrue(backend.presenter_started.wait(1))
        played = controller.snapshot()['presenter']['segments'][0]
        self.assertEqual((played['source'], played['placement']), ('Local ident', 'between_tracks'))

        other = room_session.Controller(FakeBackend(), Path(self.temp.name) / 'other')
        self.addCleanup(other.close)
        with mock.patch.object(other.presenter, '_chat') as chat:
            self.start_presenter_station(other, self.presenter_station(speech=True))
        self.assertFalse(chat.called)
        self.assertEqual(other.snapshot()['presenter']['segments'][-1]['reason'], 'primary speech source')

    def test_bounded_queue_excludes_recent_and_stops(self):
        program = station_engine.Program(station(), [track(n) for n in range(20)], seed=1)
        self.assertLessEqual(len(program.queue), station_engine.MAX_QUEUE)
        played = [program.next() for _ in range(20)]
        self.assertEqual(len({x['id'] for x in played}), 20)
        self.assertIsNone(program.next())

    def test_play_next_then_continue(self):
        program = station_engine.Program(station(), [track(n) for n in range(12)], seed=1)
        first = program.next()
        following = copy.deepcopy(program.queue[0])
        program.play_next(track(99))
        self.assertEqual(program.next()['id'], '99')
        self.assertEqual(program.next(), following)
        self.assertNotEqual(first, following)

    def test_private_urls_and_unsupported_provider_rejected(self):
        item = station()
        for parameter in ('password', 'credential', 'signature', 'authorization'):
            item['sources'] = [{'url': 'https://example.test/stream?' + parameter + '=fixture-secret'}]
            with self.assertRaises(ValueError):
                station_engine.normalize(item)
        item['sources'] = [{'url': 'https://user:secret@example.test/stream'}]
        with self.assertRaises(ValueError):
            station_engine.normalize(item)
        item['sources'] = [{'reference': {'provider': 'unknown', 'id': '1'}}]
        with self.assertRaises(ValueError):
            station_engine.normalize(item)

    def test_strict_instrumental_excludes_unknown_and_invalid_preferences(self):
        definition = station()
        definition['preferences'] = {'speech': 'Strict instrumental'}
        program = station_engine.Program(definition, [track(1), dict(track(2), speech=False),
                                                     dict(track(3), speech=False, speech_verified=True)])
        self.assertIsNone(program.next())
        with self.assertRaises(ValueError):
            program.play_next(track(1))
        definition['preferences'] = {'energy': float('nan')}
        with self.assertRaises(ValueError):
            station_engine.normalize(definition)

    def test_preferences_arcs_feedback_and_play_next_affect_future_only(self):
        definition = station()
        definition.update(arc='wind_down', preferences={'energy': 'High'})
        candidates = [dict(track(n), artist='Artist ' + str(n), energy=n / 10) for n in range(10)]
        program = station_engine.Program(definition, candidates, seed=3, trusted_resolver=lambda item: {'features': [{'name': 'energy', 'value': item.get('energy'), 'confidence': 0.9}]})
        self.assertEqual(program.next()['id'], '9')
        program.play_next(track(99))
        program.station['preferences']['energy'] = 'Low'
        program.retune()
        self.assertEqual(program.next()['id'], '99')
        self.assertEqual(program.next()['id'], '0')
        self.assertEqual(program.station['preferences']['energy'], 'Low')
        program.feedback('less')
        self.assertNotIn('0', [x['id'] for x in program.queue])
        program.hold = True
        program.station['preferences']['energy'] = 'High'
        for _ in range(3):
            program.next()
        self.assertEqual(program.station['preferences']['energy'], 'High')

    def test_jellyfin_genres_and_user_data_guide_future_order(self):
        definition = station()
        definition['preferences'] = {'energy': 'Low', 'familiarity': 'Familiar'}
        candidates = [dict(track(1), genres=['Dance'], favorite=False, play_count=0),
                      dict(track(2), genres=['Ambient'], favorite=True, play_count=2)]
        program = station_engine.Program(definition, candidates, seed=2)
        self.assertEqual(program.next()['id'], '2')
        program.station['preferences'] = {'energy': 'High', 'familiarity': 'New'}
        program.retune()
        self.assertEqual(program.next()['id'], '1')

    def test_station_eof_advances_only_on_verified_eof_and_returns_history(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            initial = controller.active['music']['reference']
            controller.refresh()
            self.assertEqual(controller.active['music']['reference'], initial)
            backend.media = {'eof_reached': True}
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertNotEqual(controller.active['music']['reference'], initial)
            history = controller.snapshot()['station_recent'][0]
            self.assertEqual(len(history['tracks']), 2)
            controller.dispatch('station.replay', {'id': history['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['music']['reference'], initial)

    def test_strict_rechecks_audio_before_player_and_invalidates_changed_review(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        old_hash, new_hash = 'a' * 64, 'b' * 64
        controller.metadata_store.observe('server', '1', old_hash)
        controller.metadata_store.review('server', '1', old_hash, 'instrumental', 'Alice')
        definition = station()
        definition['preferences'] = {'speech': 'Strict instrumental'}
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1)]), \
             mock.patch('metadata_pipeline.library_origin', return_value='server'), \
             mock.patch('metadata_pipeline.acquire_verified_snapshot', side_effect=lambda *a, **k: {'origin': 'server', 'audio_sha256': new_hash, 'file': tempfile.TemporaryFile()}):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertFalse(any(call[0] == 'start_music' for call in backend.calls))
            self.assertIsNone(controller.metadata_store.inspect('server', '1', old_hash))
            self.assertIsNone(controller.metadata_store.inspect('server', '1', new_hash)['review'])

    def test_likely_mode_analyzes_bounded_candidates_without_starting_another_track(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        definition = station()
        definition['preferences'] = {'speech': 'Likely instrumental'}
        candidates = [track(n) for n in range(10)]
        revision = {'etag': 'v1'}
        def collect(ref, cancel_event=None):
            return {'origin': 'server', 'item_id': ref['id'], 'revision': revision, 'identities': [], 'features': []}
        def classify(ref, cancel_event=None):
            return {'audio_sha256': 'a' * 64, 'revision': revision, 'origin': 'server',
                    'features': [{'provider': 'Essentia MTG', 'name': 'instrumental score',
                                  'method': 'whole original audio mean patch estimate', 'value': .9}]}
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=candidates), \
             mock.patch('metadata_pipeline.library_origin', return_value='server'), \
             mock.patch('metadata_pipeline.collect_jellyfin', side_effect=collect), \
             mock.patch('metadata_pipeline.classify_jellyfin', side_effect=classify) as classifier:
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            controller.analysis_worker.submit(lambda: None).result(timeout=3)
            controller.metadata_worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(classifier.call_count, 8)
            self.assertEqual(sum(call[0] == 'start_music' for call in backend.calls), 1)
            self.assertIsNotNone(station_engine.instrumental_score(controller.metadata_store.current('server', '0')))

    def test_strict_player_receives_verified_snapshot_and_revocation_stops_it(self):
        class SnapshotBackend(FakeBackend):
            def start_verified_music(self, source, cancel, audio_file, player_unit=None):
                self.observed = audio_file.read()
                return self.start_music(source, cancel)
        backend = SnapshotBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        digest = 'a' * 64
        controller.metadata_store.observe('server', '1', digest)
        controller.metadata_store.review('server', '1', digest, 'instrumental', 'Alice')
        definition = station()
        definition['preferences'] = {'speech': 'Strict instrumental'}
        def snapshot(*args, **kwargs):
            audio = tempfile.TemporaryFile()
            audio.write(b'exact verified bytes')
            audio.seek(0)
            return {'origin': 'server', 'audio_sha256': digest, 'file': audio}
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1)]), \
             mock.patch('metadata_pipeline.library_origin', return_value='server'), \
             mock.patch('metadata_pipeline.acquire_verified_snapshot', side_effect=snapshot):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(backend.observed, b'exact verified bytes')
            held = controller.verified_audio
            self.assertFalse(held.closed)
            controller.dispatch('metadata.revoke', {'origin': 'server', 'item_id': '1'})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertTrue(held.closed)
            self.assertIn(('music', 'stop', None), backend.calls)

    def test_review_change_or_new_revision_stops_active_strict_audio(self):
        for change in ('review', 'revision'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                backend = FakeBackend()
                controller = room_session.Controller(backend, directory)
                self.addCleanup(controller.close)
                digest = 'a' * 64
                controller.metadata_store.observe('server', '1', digest)
                controller.metadata_store.review('server', '1', digest, 'instrumental', 'Alice')
                definition = station()
                definition['preferences'] = {'speech': 'Strict instrumental'}
                def snapshot(*args, **kwargs):
                    return {'origin': 'server', 'audio_sha256': digest, 'file': tempfile.TemporaryFile()}
                with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
                     mock.patch('online_services.jellyfin_queue', return_value=[track(1)]), \
                     mock.patch('metadata_pipeline.library_origin', return_value='server'), \
                     mock.patch('metadata_pipeline.acquire_verified_snapshot', side_effect=snapshot):
                    saved = controller.dispatch('station.save', {'station': definition})['station']
                    controller.dispatch('station.start', {'id': saved['id']})
                    controller.worker.submit(lambda: None).result(timeout=3)
                    with controller.lock:
                        if change == 'review':
                            controller.metadata_store.review('server', '1', digest, 'vocal', 'Alice')
                            controller._check_pending_review()
                        else:
                            controller._metadata_observe('server', '1', 'b' * 64)
                    controller.worker.submit(lambda: None).result(timeout=3)
                    self.assertIn(('music', 'stop', None), backend.calls)
                    self.assertIsNone(controller.verified_audio)

    def test_revocation_cancels_staged_strict_player(self):
        backend = FakeBackend()
        backend.block = threading.Event()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        digest = 'a' * 64
        controller.metadata_store.observe('server', '1', digest)
        controller.metadata_store.review('server', '1', digest, 'instrumental', 'Alice')
        definition = station()
        definition['preferences'] = {'speech': 'Strict instrumental'}
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1)]), \
             mock.patch('metadata_pipeline.library_origin', return_value='server'), \
             mock.patch('metadata_pipeline.acquire_verified_snapshot', side_effect=lambda *a, **k: {'origin': 'server', 'audio_sha256': digest, 'file': tempfile.TemporaryFile()}):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            self.assertTrue(backend.started.wait(1))
            controller.dispatch('metadata.revoke', {'origin': 'server', 'item_id': '1'})
            backend.block.set()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertFalse(any(call[0] == 'start_music' for call in backend.calls))
            self.assertIsNone(controller.active)

    def test_metadata_service_returns_results_not_a_queued_acknowledgement(self):
        import session_service
        controller = room_session.Controller(FakeBackend(), self.temp.name)
        self.addCleanup(controller.close)
        service = session_service.Service(controller)
        self.assertEqual(service.execute('metadata.list', {}), {'records': [], 'truncated': False})
        with mock.patch.dict(os.environ, {'ACOUSTID_CLIENT_KEY': ''}):
            self.assertEqual(service.execute('metadata.status', {}), {'acoustid_ready': False})

    def test_acoustid_key_setup_exposes_readiness_not_key(self):
        controller = room_session.Controller(FakeBackend(), self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.dict(os.environ, {'ACOUSTID_CLIENT_KEY': ''}):
            self.assertFalse(controller.dispatch('metadata.status')['acoustid_ready'])
            response = controller.dispatch('metadata.configure_acoustid', {'client_key': 'fixture-app-key'})
            self.assertTrue(response['acoustid_ready'])
            self.assertTrue(controller.dispatch('metadata.status')['acoustid_ready'])
            self.assertNotIn('fixture-app-key', repr(controller.snapshot()))
            self.assertNotIn('fixture-app-key', repr(response))
            self.assertFalse(controller.dispatch('metadata.configure_acoustid', {'client_key': ''})['acoustid_ready'])

    def test_restart_stops_only_orphaned_verified_player(self):
        class SharedBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.owned_unit = None
                self.stopped_units = []
            def start_verified_music(self, source, cancel, audio_file, player_unit=None):
                self.owned_unit = player_unit or 'listening-hub-radio-123456abcdef.service'
                return self.start_music(source, cancel)
            def stop_owned_identity(self, identity):
                self.stopped_units.append(identity)
                if identity == self.owned_unit:
                    self.state['music']['status'] = 'Stopped'
        backend = SharedBackend()
        first = room_session.Controller(backend, self.temp.name)
        self.addCleanup(first.close)
        digest = 'a' * 64
        first.metadata_store.observe('server', '1', digest)
        first.metadata_store.review('server', '1', digest, 'instrumental', 'Alice')
        definition = station()
        definition['preferences'] = {'speech': 'Strict instrumental'}
        def snapshot(*args, **kwargs):
            return {'origin': 'server', 'audio_sha256': digest, 'file': tempfile.TemporaryFile()}
        with mock.patch.object(first, '_home_action'), mock.patch.object(first, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1)]), \
             mock.patch('metadata_pipeline.library_origin', return_value='server'), \
             mock.patch('metadata_pipeline.acquire_verified_snapshot', side_effect=snapshot):
            saved = first.dispatch('station.save', {'station': definition})['station']
            first.dispatch('station.start', {'id': saved['id']})
            first.worker.submit(lambda: None).result(timeout=3)
            identity = backend.owned_unit
            self.assertEqual(backend.state['music']['status'], 'Playing')
            first.close()
            second = room_session.Controller(backend, self.temp.name)
            self.addCleanup(second.close)
            second.worker.submit(lambda: None).result(timeout=3)
            self.assertIn(identity, backend.stopped_units)
            self.assertEqual(backend.state['music']['status'], 'Stopped')

    def test_recovery_marker_does_not_stop_unrelated_player(self):
        class SharedBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.state['music']['status'] = 'Playing'
                self.current_unit = 'listening-hub-radio-ffffffffffff.service'
                self.stopped_units = []
            def stop_owned_identity(self, unit):
                self.stopped_units.append(unit)
                if unit == self.current_unit:
                    self.state['music']['status'] = 'Stopped'
        old_unit = 'listening-hub-radio-123456abcdef.service'
        room_session.hub.save_json(Path(self.temp.name) / 'verified-playback.json', [{'unit': old_unit, 'origin': 'server', 'item_id': '1', 'audio_sha256': 'a' * 64}])
        backend = SharedBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        controller.worker.submit(lambda: None).result(timeout=3)
        self.assertEqual(backend.stopped_units, [old_unit])
        self.assertEqual(backend.state['music']['status'], 'Playing')
        self.assertEqual(controller.verified_units, [])

    def test_failed_recovery_retains_marker_and_blocks_new_entry(self):
        class FailedRecovery(FakeBackend):
            def stop_owned_identity(self, unit):
                raise ValueError('fixture failure')
        marker = [{'unit': 'listening-hub-radio-123456abcdef.service', 'origin': 'server', 'item_id': '1', 'audio_sha256': 'a' * 64}]
        room_session.hub.save_json(Path(self.temp.name) / 'verified-playback.json', marker)
        backend = FailedRecovery()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        controller.worker.submit(lambda: None).result(timeout=3)
        self.assertTrue(controller.recovery_blocked)
        self.assertEqual(room_session.hub.load_json(Path(self.temp.name) / 'verified-playback.json', []), marker)
        saved = controller.dispatch('station.save', {'station': station()})['station']
        controller.dispatch('station.start', {'id': saved['id']})
        controller.worker.submit(lambda: None).result(timeout=3)
        self.assertFalse(any(call[0] == 'start_music' for call in backend.calls))

    def test_invalid_recovery_marker_fails_closed_without_rewriting(self):
        for case in ('malformed', 'too_many', 'unreadable'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                marker_path = Path(directory) / 'verified-playback.json'
                if case == 'malformed':
                    marker_path.write_text('{invalid', encoding='utf-8')
                elif case == 'too_many':
                    entries = [{'unit': f'listening-hub-radio-{i:012x}.service', 'origin': 'server', 'item_id': str(i), 'audio_sha256': 'a' * 64} for i in range(21)]
                    room_session.hub.save_json(marker_path, entries)
                else:
                    marker_path.mkdir()
                before = marker_path.read_bytes() if marker_path.is_file() else None
                backend = FakeBackend()
                controller = room_session.Controller(backend, directory)
                self.addCleanup(controller.close)
                self.assertTrue(controller.recovery_blocked)
                saved = controller.dispatch('station.save', {'station': station()})['station']
                controller.dispatch('station.start', {'id': saved['id']})
                controller.worker.submit(lambda: None).result(timeout=3)
                self.assertFalse(any(call[0] == 'start_music' for call in backend.calls))
                self.assertEqual(marker_path.read_bytes() if marker_path.is_file() else None, before)

    def test_failed_recovery_marker_write_keeps_all_units_and_blocks_entry(self):
        class RecoveringBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.stopped_units = []
            def stop_owned_identity(self, unit):
                self.stopped_units.append(unit)
        entries = [{'unit': f'listening-hub-radio-{i:012x}.service', 'origin': 'server', 'item_id': str(i), 'audio_sha256': 'a' * 64} for i in range(2)]
        marker = Path(self.temp.name) / 'verified-playback.json'
        room_session.hub.save_json(marker, entries)
        backend = RecoveringBackend()
        original_save = room_session.hub.save_json
        def fail_marker(path, value):
            if Path(path) == marker:
                raise OSError('fixture write failure')
            return original_save(path, value)
        with mock.patch.object(room_session.hub, 'save_json', side_effect=fail_marker):
            controller = room_session.Controller(backend, self.temp.name)
            self.addCleanup(controller.close)
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(backend.stopped_units, [entry['unit'] for entry in entries])
            self.assertTrue(controller.recovery_blocked)
            self.assertEqual(controller.verified_units, entries)
            self.assertEqual(room_session.hub.load_json(marker, []), entries)
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertFalse(any(call[0] == 'start_music' for call in backend.calls))

    def test_cancelled_load_never_creates_station_history(self):
        backend = FakeBackend()
        backend.block = __import__('threading').Event()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            self.assertTrue(backend.started.wait(1))
            controller.stop()
            backend.block.set()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.station_history, [])

    def test_interruption_reconnect_is_bounded_and_quiet_never_restarts(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            backend.state['music']['status'] = 'Stopped'
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), 2)
            backend.state['music']['status'] = 'Stopped'
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), 2)
            controller.stop()
            controller.refresh()
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), 2)

    def test_concurrent_station_saves_do_not_lose_updates(self):
        controller = room_session.Controller(FakeBackend(), self.temp.name)
        self.addCleanup(controller.close)
        barrier = threading.Barrier(12)
        errors = []
        def save(n):
            try:
                definition = station()
                definition['name'] = 'Station ' + str(n)
                barrier.wait()
                controller.dispatch('station.save', {'station': definition})
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=save, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(controller.dispatch('station.list')['stations']), 12)

    def test_audiomuse_failure_uses_only_explicit_fallback(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        definition = station()
        definition['generation'] = {'kind': 'audiomuse'}
        definition['fallbacks'] = [{'source_id': 'approved', 'name': 'Approved', 'url': 'https://example.test/live'}]
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.audiomuse_similar', side_effect=ValueError('unavailable')):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertIn(('start_music', 'approved'), backend.calls)
            self.assertIsNone(controller.program)

    def test_untrusted_library_reference_cannot_persist_secret_or_forge_speech(self):
        definition = station()
        ref = definition['sources'][0]['reference']
        ref.update(uri='https://example.test/?token=fixture-secret', speech=False, speech_verified=True)
        cleaned = station_engine.normalize(definition)['sources'][0]['reference']
        self.assertNotIn('uri', cleaned)
        self.assertNotIn('speech_verified', cleaned)
        ref['id'] = 'bad?token=fixture-secret'
        with self.assertRaises(ValueError):
            station_engine.normalize(definition)

    def test_stopped_player_with_verified_eof_advances_not_reconnects(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            first = controller.active['music']['reference']['id']
            backend.state['music']['status'] = 'Stopped'
            backend.media = {'eof_reached': True}
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertNotEqual(controller.active['music']['reference']['id'], first)

    def test_failed_next_restores_queue_and_does_not_record_failed_track(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            before = controller.snapshot()['station']
            backend.fail_music = True
            controller.queue_step(1)
            controller.worker.submit(lambda: None).result(timeout=3)
            after = controller.snapshot()['station']
            self.assertEqual(after['upcoming'], before['upcoming'])
            self.assertEqual(after['recent'], before['recent'])
            self.assertEqual(after['current'], before['current'])

    def test_quiet_during_next_keeps_unplayed_track_in_queue(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            before = controller.snapshot()['station']
            backend.started.clear()
            backend.block = threading.Event()
            controller.queue_step(1)
            self.assertTrue(backend.started.wait(1))
            controller.quiet()
            backend.block.set()
            controller.worker.submit(lambda: None).result(timeout=3)
            after = controller.snapshot()['station']
            self.assertEqual(after['upcoming'], before['upcoming'])
            self.assertEqual(after['current'], before['current'])

    def test_explicit_music_change_leaves_station_even_for_same_library_id(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            controller.dispatch('online', {'item': track(20)})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertNotIn('station', controller.active['music'])
            self.assertIsNone(controller.program)
            self.assertEqual(controller.active['music']['reference']['id'], '20')

    def test_sources_rotate_and_memory_survives_restart(self):
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        definition = station()
        definition['sources'].append({'source_id': 'other', 'name': 'Other', 'url': 'https://example.test/radio'})
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3)]):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            first = controller.active['music']['reference']['id']
            controller.dispatch('station.feedback', {'rating': 'less'})
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['music']['source_id'], 'other')
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertNotEqual(controller.active['music']['reference']['id'], first)
            self.assertIn(first, [entry[1] for entry in controller.station_memory[saved['id']]['disliked']])

    def test_background_library_advances_at_eof_without_restarting_music(self):
        class BackgroundBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.background_media = {}
            def atmosphere_details(self):
                return copy.deepcopy(self.background_media)
            def start_atmosphere(self, selection, cancel):
                ref = selection['reference']
                queue = [track(1), track(2)] if ref['type'] == 'MusicAlbum' else [ref]
                actual = {'id': 'bg-' + queue[0]['id'], 'reference': ref, 'queue': queue}
                self.state['atmosphere'] = {'status': 'Playing', 'source_id': actual['id'], 'volume': 20, 'muted': False}
                self.background_media = {}
                return actual
        backend = BackgroundBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        album = station()['sources'][0]['reference']
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'):
            controller.enter({'music': {'source_id': 'direct', 'url': 'https://example.test/audio', 'name': 'Main'},
                              'atmosphere': {'kind': 'radio', 'reference': album, 'volume': 20}})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['atmosphere']['reference']['id'], '1')
            starts = sum(c[0] == 'start_music' for c in backend.calls)
            backend.background_media = {'eof_reached': True}
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['atmosphere']['reference']['id'], '2')
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), starts)

    def test_library_fallback_continues_its_own_queue(self):
        class FallbackBackend(FakeBackend):
            def start_music(self, source, cancel):
                ref = source.get('reference') or {}
                if ref.get('id') == 'backup':
                    source = dict(source, queue=[track(11), track(12)], queue_index=0)
                return super().start_music(source, cancel)
        backend = FallbackBackend()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        definition = station()
        definition['generation'] = {'kind': 'audiomuse'}
        definition['fallbacks'] = [{'reference': {'provider': 'jellyfin', 'id': 'backup', 'type': 'MusicAlbum', 'name': 'Backup'}}]
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.audiomuse_similar', side_effect=ValueError('unavailable')):
            saved = controller.dispatch('station.save', {'station': definition})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['music']['reference']['id'], '11')
            self.assertEqual(controller.program.queue[0]['id'], '12')
            controller.queue_step(1)
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['music']['reference']['id'], '12')

    def test_stable_playback_reenables_a_later_reconnect(self):
        now = [1000.]
        backend = FakeBackend()
        controller = room_session.Controller(backend, self.temp.name, clock=lambda: now[0])
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            backend.state['music']['status'] = 'Stopped'
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            now[0] += 61
            controller.refresh()
            backend.state['music']['status'] = 'Stopped'
            controller.refresh()
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), 3)

    def test_station_feedback_and_recent_exclusion_survive_controller_restart(self):
        first_backend = FakeBackend()
        controller = room_session.Controller(first_backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3)]):
            saved = controller.dispatch('station.save', {'station': station()})['station']
            controller.dispatch('station.start', {'id': saved['id']})
            controller.worker.submit(lambda: None).result(timeout=3)
            first = controller.active['music']['reference']['id']
            controller.dispatch('station.feedback', {'rating': 'less'})
        controller.close()
        other = room_session.Controller(FakeBackend(), self.temp.name)
        self.addCleanup(other.close)
        with mock.patch.object(other, '_home_action'), mock.patch.object(other, '_route_preferred'), \
             mock.patch('online_services.jellyfin_queue', return_value=[track(1), track(2), track(3)]):
            self.assertIsNone(other.active)
            other.dispatch('station.start', {'id': saved['id']})
            other.worker.submit(lambda: None).result(timeout=3)
            self.assertNotEqual(other.active['music']['reference']['id'], first)
            self.assertIn(first, [entry[1] for entry in other.station_memory[saved['id']]['disliked']])

    def test_background_failed_next_keeps_queue_and_never_advances_music(self):
        class FailingBackground(FakeBackend):
            def start_atmosphere(self, selection, cancel):
                if selection.get('queue_index') == 1:
                    raise ValueError('Background unavailable')
                ref = selection['reference']
                self.state['atmosphere'] = {'status': 'Playing', 'source_id': 'bg-1', 'volume': 20, 'muted': False}
                return {'id': 'bg-1', 'reference': ref, 'queue': [track(1), track(2)]}
        backend = FailingBackground()
        controller = room_session.Controller(backend, self.temp.name)
        self.addCleanup(controller.close)
        with mock.patch.object(controller, '_home_action'), mock.patch.object(controller, '_route_preferred'):
            controller.enter({'music': {'source_id': 'direct', 'url': 'https://example.test/audio', 'name': 'Main'},
                              'atmosphere': {'kind': 'radio', 'reference': station()['sources'][0]['reference']}})
            controller.worker.submit(lambda: None).result(timeout=3)
            starts = sum(c[0] == 'start_music' for c in backend.calls)
            controller.layer({'layer': 'atmosphere', 'action': 'next'})
            controller.worker.submit(lambda: None).result(timeout=3)
            self.assertEqual(controller.active['atmosphere']['reference']['id'], '1')
            self.assertIn('unavailable', controller.errors['atmosphere'])
            self.assertEqual(sum(c[0] == 'start_music' for c in backend.calls), starts)


if __name__ == '__main__':
    unittest.main()
