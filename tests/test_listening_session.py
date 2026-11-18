"""Ownership, cancellation and failure controls for the shared room session."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import room_session as sessions
import session_client
import session_service


class FakeBackend:
    def __init__(self):
        self.state = {name: {'status': 'Stopped'} for name in ('music', 'atmosphere', 'picture')}
        self.calls = []
        self.fail_music = False
        self.fail_picture = False
        self.started = threading.Event()
        self.block = None
        self.picture_block = None
        self.picture_started = threading.Event()
        self.media = {}

    def layers(self):
        return copy.deepcopy(self.state)

    def start_music(self, source, cancel):
        self.started.set()
        if self.block:
            self.block.wait(2)
        if cancel.is_set():
            raise ValueError('Cancelled before source commit')
        if self.fail_music:
            raise ValueError('Station could not load')
        if source.get('reference', {}).get('provider') == 'jellyfin':
            source = dict(source, id='jellyfin-' + source['reference']['id'], name=source['reference']['name'])
        self.media = {}
        self.calls.append(('start_music', source['id']))
        self.state['music'] = {'status': 'Playing', 'source_id': source['id'], 'name': source['name'], 'volume': 50, 'muted': False}
        return source

    def start_verified_music(self, source, cancel, audio_file, player_unit=None):
        return self.start_music(source, cancel)

    def media_details(self):
        return copy.deepcopy(self.media)

    def music_control(self, action, value=None):
        self.calls.append(('music', action, value))
        self._control('music', action, value)

    def start_atmosphere(self, selection, cancel):
        if cancel.is_set():
            raise ValueError('Cancelled')
        self.calls.append(('start_atmosphere', copy.deepcopy(selection)))
        self.state['atmosphere'] = dict(selection, status='Stopped' if selection.get('kind') == 'none' else 'Playing')

    def atmosphere_control(self, action, value=None):
        self.calls.append(('atmosphere', action, value))
        self._control('atmosphere', action, value)

    def start_picture(self, picture, cancel=None):
        self.picture_started.set()
        if self.picture_block:
            self.picture_block.wait(2)
        if cancel is not None and cancel.is_set():
            raise ValueError('Picture start cancelled')
        if self.fail_picture:
            raise ValueError('No monitor')
        self.calls.append(('start_picture', copy.deepcopy(picture)))
        self.state['picture'] = dict(picture, status='Playing' if picture.get('enabled') else 'Stopped')

    def picture_control(self, action, value=None):
        self.calls.append(('picture', action, value))
        self._control('picture', action, value)

    def extra_video_control(self, action):
        self.calls.append(('owned_video', action))

    def _control(self, layer, action, value):
        if action in ('pause', 'play', 'stop'):
            self.state[layer]['status'] = {'pause': 'Paused', 'play': 'Playing', 'stop': 'Stopped'}[action]
        elif action in ('volume', 'muted'):
            self.state[layer][action] = value
        elif action == 'window':
            self.state[layer]['window_open'] = value
        elif action == 'mute':
            self.state[layer]['muted'] = not self.state[layer].get('muted', False)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'),
            'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.backend = FakeBackend()
        self.now = 1000.
        self.controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        self.sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'},
                        {'id': 'ambient', 'name': 'Ambient', 'kind': 'radio', 'url': 'https://example.test/ambient'}]
        self.rooms = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library',
                       'poster_path': '/artwork/library.jpg', 'accent': '#c79b68'},
                      {'id': 'north', 'name': 'Northern quiet', 'source_id': 'ambient', 'view_id': 'room-north'}]
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=self.sources),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=self.rooms),
                      mock.patch.object(self.controller, '_home_action'),
                      mock.patch.object(self.controller, '_route_preferred')):
            patch.start()
            self.addCleanup(patch.stop)

    def settle(self):
        self.controller.worker.submit(lambda: None).result(timeout=3)
        self.controller.refresh()

    def enter(self, identity='library', **kwargs):
        self.controller.enter(dict(room_id=identity, **kwargs))
        self.settle()

    def test_enter_is_explicit_and_save_roundtrips_all_three_layers(self):
        self.assertIsNone(self.controller.snapshot()['active'])
        self.assertEqual(self.backend.calls, [])
        self.enter(atmosphere={'kind': 'local', 'source_id': 'rain', 'volume': 18,
                               'muted': False, 'window_open': 40},
                   picture={'still': True, 'placement': 'pip', 'display': 'DP-1'})
        self.backend.state['music']['volume'] = 31
        self.backend.state['atmosphere']['volume'] = 12
        self.controller.refresh()
        saved = self.controller.save({'name': 'Midnight Library · Rainy reading'})['variant']
        self.assertEqual(saved['music']['source_id'], 'piano')
        self.assertEqual(saved['music']['volume'], 31)
        self.assertEqual(saved['atmosphere']['source_id'], 'rain')
        self.assertEqual(saved['atmosphere']['volume'], 12)
        self.assertEqual(saved['atmosphere']['window_open'], 40)
        self.assertEqual(saved['picture'], {'preset_id': 'room-library', 'enabled': True,
                         'still': True, 'placement': 'pip', 'display': 'DP-1'})
        before = list(self.backend.calls)
        reopened = sessions.Controller(self.backend, self.tmp.name)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.snapshot()['active'])
        self.assertEqual(reopened.snapshot()['last_session']['id'], saved['id'])
        self.assertEqual(before, self.backend.calls, 'Reopening must not autoplay')

    def test_failed_music_load_keeps_previous_active_room(self):
        self.enter()
        self.backend.fail_music = True
        self.enter('north')
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['active']['id'], 'library')
        self.assertEqual(snapshot['layers']['music']['source_id'], 'piano')
        self.assertIn('could not load', snapshot['errors']['music'])

    def test_missing_source_fails_before_invalidating_or_playback(self):
        self.enter()
        original = self.controller.generation
        with self.assertRaisesRegex(sessions.SessionError, 'source unavailable'):
            self.controller.enter({'music': {'source_id': 'missing'}})
        self.assertEqual(original, self.controller.generation)
        self.assertEqual(self.controller.active['id'], 'library')

    def test_quiet_cancels_pending_entry_before_its_music_commit(self):
        self.enter()
        self.backend.block = threading.Event()
        self.backend.started.clear()
        self.controller.enter({'room_id': 'north'})
        self.assertTrue(self.backend.started.wait(1))
        result = self.controller.quiet(10)
        self.backend.block.set()
        self.settle()
        self.assertEqual(result['paused'], ['music', 'picture'])
        self.assertEqual(self.controller.active['id'], 'library')
        self.assertEqual(self.backend.state['music']['status'], 'Paused')
        self.assertNotIn(('start_music', 'ambient'), self.backend.calls)

    def test_timed_quiet_only_offers_resume_and_never_resumes_itself(self):
        self.enter(atmosphere={'kind': 'local', 'source_id': 'rain'})
        self.controller.quiet(10)
        self.now += 601
        self.controller.refresh()
        snapshot = self.controller.snapshot()
        self.assertTrue(snapshot['resume_offer']['ready'])
        self.assertEqual(snapshot['layers']['music']['status'], 'Paused')
        self.controller.resume()
        self.assertEqual(self.backend.state['music']['status'], 'Playing')
        self.assertIsNone(self.controller.resume_offer)

    def test_new_room_invalidates_old_resume_offer(self):
        self.enter()
        self.controller.quiet()
        self.enter('north')
        with self.assertRaisesRegex(sessions.SessionError, 'no paused room'):
            self.controller.resume()

    def test_picture_failure_is_local_and_music_remains_active(self):
        self.backend.fail_picture = True
        self.enter()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['active']['id'], 'library')
        self.assertEqual(snapshot['layers']['music']['status'], 'Playing')
        self.assertEqual(snapshot['errors']['picture'], 'No monitor')
        self.assertNotIn('music', snapshot['errors'])

    def test_stopped_atmosphere_metadata_cannot_reappear_in_saved_recipe(self):
        self.enter()
        self.backend.state['atmosphere'] = {'status': 'Stopped', 'kind': 'local', 'source_id': 'old-rain',
                                             'volume': 20, 'muted': False}
        self.controller.refresh()
        saved = self.controller.save({'name': 'Quiet library'})['variant']
        self.assertEqual(saved['atmosphere']['kind'], 'none')
        self.assertNotIn('source_id', saved['atmosphere'])

    def test_invalid_interruption_does_not_pause_anything(self):
        self.enter()
        before = list(self.backend.calls)
        with self.assertRaises(sessions.SessionError):
            self.controller.quiet(float('nan'))
        self.assertEqual(before, self.backend.calls)

    def test_stop_uses_only_owned_layer_operations(self):
        self.enter(atmosphere={'kind': 'local', 'source_id': 'rain'})
        self.backend.calls.clear()
        self.controller.stop()
        self.assertEqual({row[0] for row in self.backend.calls}, {'music', 'atmosphere', 'picture', 'owned_video'})
        self.assertIsNone(self.controller.active)
        self.assertIsNotNone(self.controller.last_session)

    def test_reduced_motion_is_applied_to_new_rooms(self):
        self.controller.apply_settings({'reduced_motion': True, 'placement': 'here'})
        self.enter()
        self.assertTrue(self.controller.active['picture']['still'])
        self.assertEqual(self.controller.active['picture']['placement'], 'here')

    def test_picture_mode_change_and_atmosphere_do_not_restart_music(self):
        self.enter()
        self.backend.calls.clear()
        self.controller.dispatch('picture', {'action': 'still', 'still': True})
        self.controller.dispatch('atmosphere', {'kind': 'local', 'source_id': 'rain', 'volume': 15})
        self.assertFalse(any(row[0] == 'start_music' for row in self.backend.calls))
        self.assertTrue(self.controller.active['picture']['still'])
        self.assertEqual(self.controller.active['atmosphere']['source_id'], 'rain')

    def test_stop_during_picture_load_cannot_resurrect_late_picture(self):
        self.backend.picture_block = threading.Event()
        self.controller.enter({'room_id': 'library'})
        self.assertTrue(self.backend.picture_started.wait(1))
        self.controller.stop()
        self.backend.picture_block.set()
        self.settle()
        self.assertIsNone(self.controller.active)
        self.assertEqual(self.backend.state['picture']['status'], 'Stopped')
        self.assertEqual(self.backend.state['music']['status'], 'Stopped')

    def test_private_favorite_is_saved_only_by_source_reference(self):
        secret = 'secret-marker-must-never-be-copied'
        self.sources.append({'id': 'private-favorite', 'name': 'Private station', 'kind': 'radio',
                             'url': 'https://example.test/stream?token=' + secret})
        self.controller.enter({'music': {'source_id': 'private-favorite'}})
        self.settle()
        self.controller.save({'name': 'Private station room'})
        serialized = json.dumps(self.controller.snapshot())
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, (Path(self.tmp.name) / 'session.json').read_text())
        self.assertNotIn(secret, (Path(self.tmp.name) / 'room-variants.json').read_text())
        self.assertEqual(self.controller.active['music']['source_id'], 'private-favorite')

    def test_library_relay_capability_is_not_persisted_or_exposed(self):
        ref = {'provider': 'jellyfin', 'id': 'abc', 'type': 'Audio', 'name': 'A track'}
        prepared = {'id': 'jellyfin-abc', 'kind': 'radio', 'name': 'A track', 'reference': ref,
                    'url': 'http://127.0.0.1:12345/private-relay-capability',
                    'mpv_options': ['--include=/private/auth.conf']}
        with mock.patch.object(self.backend, 'start_music', return_value=prepared):
            self.controller.dispatch('online', {'item': ref})
            self.settle()
        saved = self.controller.save({'name': 'Library room'})
        serialized = json.dumps(self.controller.snapshot()) + json.dumps(saved)
        self.assertNotIn('private-relay-capability', serialized)
        self.assertNotIn('auth.conf', serialized)
        self.assertEqual(self.controller.active['music']['reference'], ref)

    def test_repeated_disconnect_does_not_erase_original_resume_offer(self):
        self.enter()
        import audio_devices
        with mock.patch.object(audio_devices, 'poll_policy', return_value={'disconnect': True, 'call_active': False}):
            self.controller.poll_policy()
            first = copy.deepcopy(self.controller.resume_offer)
            self.controller.refresh()
            self.controller.poll_policy()
        self.assertEqual(first, self.controller.resume_offer)
        self.assertIn('music', first['layers'])

    def test_call_end_respects_volume_changed_during_call(self):
        self.enter(atmosphere={'kind': 'local', 'source_id': 'rain', 'volume': 20})
        self.controller.settings['duck_on_call'] = True
        import audio_devices
        with mock.patch.object(audio_devices, 'poll_policy', return_value={'call_active': True, 'duck': True}):
            self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 10)
        self.assertEqual(self.backend.state['atmosphere']['volume'], 4)
        self.controller.layer({'layer': 'music', 'action': 'volume', 'value': 37})
        with mock.patch.object(audio_devices, 'poll_policy', return_value={'call_active': False}):
            self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 37)
        self.assertEqual(self.backend.state['atmosphere']['volume'], 20)

    def test_browser_discovery_link_fails_before_picture_or_music_effects(self):
        before = list(self.backend.calls)
        with self.assertRaisesRegex(sessions.SessionError, 'cannot be sent'):
            self.controller.open_link({'url': 'https://www.youtube.com/results?search_query=piano', 'destination': 'picture'})
        self.assertEqual(before, self.backend.calls)

    def test_stale_queued_atmosphere_action_cannot_start_after_stop(self):
        generation, cancel = self.controller.generation, self.controller.cancel
        self.controller.stop()
        self.backend.calls.clear()
        result = self.controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain',
                                        '_request_generation': generation, '_cancel_event': cancel})
        self.assertIn('cancelled', result['message'])
        self.assertEqual(self.backend.calls, [])

    def enter_library_queue(self):
        queue = [{'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'First track'},
                 {'provider': 'jellyfin', 'id': 'track2', 'type': 'Audio', 'name': 'Second track'}]
        self.controller.enter({'music': {'reference': queue[0], 'source_id': 'jellyfin-track1',
                                         'name': 'First track', 'queue': queue, 'queue_index': 0}})
        self.settle()
        return queue

    def test_intentional_music_stop_does_not_advance_library_queue(self):
        self.enter_library_queue()
        before = len([call for call in self.backend.calls if call[0] == 'start_music'])
        self.controller.layer({'layer': 'music', 'action': 'stop'})
        self.settle()
        self.assertEqual(self.controller.active['music']['queue_index'], 0)
        self.assertEqual(self.backend.state['music']['status'], 'Stopped')
        self.assertEqual(before, len([call for call in self.backend.calls if call[0] == 'start_music']))

    def test_disappeared_player_does_not_advance_library_queue(self):
        self.enter_library_queue()
        self.backend.state['music']['status'] = 'Stopped'
        self.controller.refresh()
        self.settle()
        self.assertEqual(self.controller.active['music']['queue_index'], 0)
        self.assertEqual(self.backend.state['music']['status'], 'Stopped')

    def test_positive_eof_from_current_library_source_advances_queue(self):
        self.enter_library_queue()
        self.backend.state['music']['status'] = 'Paused'
        self.backend.media = {'eof_reached': True, 'seekable': True, 'position': 100, 'duration': 100}
        self.controller.refresh()
        self.settle()
        self.assertEqual(self.controller.active['music']['queue_index'], 1)
        self.assertEqual(self.backend.state['music']['source_id'], 'jellyfin-track2')
        self.assertEqual(self.backend.state['music']['status'], 'Playing')

    def test_eof_from_a_different_source_cannot_advance_current_library(self):
        self.enter_library_queue()
        self.backend.state['music'].update(status='Paused', source_id='some-other-source')
        self.backend.media = {'eof_reached': True}
        self.controller.refresh()
        self.settle()
        self.assertEqual(self.controller.active['music']['queue_index'], 0)

    def test_save_waits_behind_previously_accepted_layer_update(self):
        self.enter()
        started, release = threading.Event(), threading.Event()
        original = self.backend.music_control
        def slow_volume(action, value=None):
            if action == 'volume':
                started.set()
                release.wait(2)
            return original(action, value)
        service = session_service.Service(self.controller)
        with mock.patch.object(self.backend, 'music_control', side_effect=slow_volume):
            service.execute('layer', {'layer': 'music', 'action': 'volume', 'value': 23})
            self.assertTrue(started.wait(1))
            result = service.execute('save', {'name': 'After slider'})
            self.assertTrue(result['accepted'])
            self.assertEqual(self.controller.variants(), [])
            release.set()
            self.settle()
        self.assertEqual(self.controller.variants()[0]['music']['volume'], 23)

    def test_failed_routing_write_does_not_change_session_preferences(self):
        self.enter()
        import audio_devices
        before = copy.deepcopy(self.controller.settings)
        durable = (Path(self.tmp.name) / 'session.json').read_bytes()
        with mock.patch.object(audio_devices, 'configure', side_effect=PermissionError('Routing store is read-only')):
            with self.assertRaisesRegex(PermissionError, 'read-only'):
                self.controller.apply_settings({'preferred_output': 'headphones'})
        self.assertEqual(self.controller.settings, before)
        self.assertEqual((Path(self.tmp.name) / 'session.json').read_bytes(), durable)

    def test_failed_session_write_rolls_back_audio_preferences(self):
        self.enter()
        import audio_devices
        audio_devices.configure(preferred_output='speakers', pause_on_disconnect=False, duck_on_call=False)
        before = audio_devices.settings()
        session_before = copy.deepcopy(self.controller.settings)
        with mock.patch.object(self.controller, '_persist', side_effect=PermissionError('Session store is read-only')):
            with self.assertRaisesRegex(PermissionError, 'read-only'):
                self.controller.apply_settings({'preferred_output': 'headphones'})
        self.assertEqual(audio_devices.settings(), before)
        self.assertEqual(self.controller.settings, session_before)

    def test_failed_eof_advance_keeps_error_and_does_not_retry_in_a_loop(self):
        self.enter_library_queue()
        import audio_devices
        self.backend.state['music']['status'] = 'Paused'
        self.backend.media = {'eof_reached': True, 'seekable': True, 'position': 100, 'duration': 100}
        generation = self.controller.generation
        with mock.patch.object(audio_devices, 'configure', side_effect=PermissionError('Routing store is read-only')) as write:
            self.controller.refresh()
            self.settle()
            self.assertEqual(self.controller.active['music']['queue_index'], 0)
            self.assertIn('read-only', self.controller.errors['music'])
            self.assertEqual(self.controller.generation, generation + 1)
            self.controller.refresh()
            self.settle()
            self.assertEqual(self.controller.generation, generation + 1)
            write.assert_called_once()

    def test_routing_configuration_is_written_only_under_test_directory(self):
        import audio_devices
        self.controller.apply_settings({'preferred_output': None, 'pause_on_disconnect': True})
        path = Path(os.environ['XDG_CONFIG_HOME']) / 'listening-hub' / 'audio.json'
        self.assertTrue(path.is_relative_to(self.tmp.name))
        self.assertTrue(path.is_file())


class MprisTests(unittest.TestCase):
    def test_live_radio_never_advertises_skip_or_seek_even_if_decoder_seekable(self):
        values = session_service.mpris_values({'layers': {'music': {'status': 'Playing', 'source_id': 'piano', 'volume': 30}},
                                               'media': {'seekable': True, 'duration': 100, 'position': 10}})
        self.assertTrue(values['CanPause'])
        self.assertFalse(values['CanGoNext'])
        self.assertFalse(values['CanGoPrevious'])
        self.assertFalse(values['CanSeek'])
        self.assertEqual(values['Position'], 0)
        self.assertEqual(values['Volume'], .3)

    def test_known_seekable_video_exposes_position_and_stable_track_identity(self):
        snapshot = {'layers': {'music': {'status': 'Paused', 'source_id': 'youtube-video', 'title': 'A video'}},
                    'media': {'seekable': True, 'duration': 100, 'position': 10}}
        values = session_service.mpris_values(snapshot)
        self.assertTrue(values['CanSeek'])
        self.assertEqual(values['Position'], 10_000_000)
        self.assertEqual(values['Metadata']['mpris:length'], 100_000_000)
        self.assertEqual(values['Metadata']['mpris:trackid'], session_service.mpris_values(snapshot)['Metadata']['mpris:trackid'])

    def test_stopped_service_has_no_track_or_spurious_play_capability(self):
        values = session_service.mpris_values({'layers': {}})
        self.assertEqual(values['Metadata'], {})
        self.assertFalse(values['CanPlay'])
        self.assertFalse(values['CanPause'])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'session.sock'
        controller = mock.Mock()
        controller.dispatch.side_effect = lambda action, args: {'action': action, 'args': args}
        self.service = session_service.Service(controller)
        self.server, self.fd = session_service.make_server(self.path, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        os.close(self.fd)

    def test_private_socket_roundtrip_and_single_instance_lock(self):
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        self.assertEqual(session_client._exchange(self.path, 'inspect', {}, 1), {'action': 'inspect', 'args': {}})
        duplicate, descriptor = session_service.make_server(self.path, self.service)
        self.assertIsNone(duplicate)
        self.assertIsNone(descriptor)

    def test_malformed_request_is_rejected_without_dispatch(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.path))
            connection.sendall(b'{"action":"inspect","args":[1]}\n')
            response = json.loads(connection.recv(4096))
        self.assertFalse(response['ok'])
        self.service.controller.dispatch.assert_not_called()

    def test_unexpected_non_socket_path_is_never_replaced(self):
        path = self.path.parent / 'wrong.sock'
        path.write_text('keep me')
        with self.assertRaisesRegex(sessions.SessionError, 'unexpected'):
            session_service.make_server(path, self.service)
        self.assertEqual(path.read_text(), 'keep me')


if __name__ == '__main__':
    unittest.main()
