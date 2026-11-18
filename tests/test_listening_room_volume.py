"""Room volume: one session multiplier over Music and Ambience, shared by every surface."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_session as sessions
import roomctl
import session_client
import session_service
from test_listening_session import FakeBackend


class RoomVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'),
            'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)
        self.backend = FakeBackend()
        self.controller = self.make_controller()
        self.sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'}]
        self.rooms = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'}]
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=self.sources),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=self.rooms)):
            patch.start()
            self.addCleanup(patch.stop)

    def make_controller(self):
        controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: 1000.)
        controller._home_action = mock.Mock()
        controller._route_preferred = mock.Mock()
        self.addCleanup(controller.close)
        return controller

    def settle(self):
        self.controller.worker.submit(lambda: None).result(timeout=3)
        self.controller.refresh()

    def enter(self, **extra):
        self.controller.enter(dict(room_id='library', atmosphere={'kind': 'local', 'source_id': 'rain', 'volume': 20}, **extra))
        self.settle()

    def test_full_room_volume_leaves_layer_levels_untouched(self):
        self.enter()
        self.assertEqual(self.controller.room_volume, 100)
        self.assertEqual(self.backend.state['music']['volume'], 50)
        self.assertEqual(self.backend.state['atmosphere']['volume'], 20)
        self.assertEqual(self.controller.snapshot()['room_volume'], 100)

    def test_room_volume_scales_both_layers_and_keeps_their_ratio(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 40})
        self.assertEqual(self.backend.state['music']['volume'], 20)
        self.assertEqual(self.backend.state['atmosphere']['volume'], 8)
        self.assertEqual(self.backend.state['music']['volume'] / self.backend.state['atmosphere']['volume'], 50 / 20)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['room_volume'], 40)
        self.assertEqual(snapshot['levels'], {'music': 50, 'atmosphere': 20})
        self.assertEqual(self.controller.active['music']['volume'], 50)
        self.controller.dispatch('room_volume', {'value': 100})
        self.assertEqual((self.backend.state['music']['volume'], self.backend.state['atmosphere']['volume']), (50, 20))

    def test_layer_level_changes_are_scaled_and_remembered_as_levels(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 50})
        self.controller.layer({'layer': 'music', 'action': 'volume', 'value': 60})
        self.assertEqual(self.backend.state['music']['volume'], 30)
        self.assertEqual(self.controller.active['music']['volume'], 60)
        self.assertEqual(self.controller.snapshot()['levels']['music'], 60)

    def test_mute_is_independent_of_room_volume(self):
        self.enter()
        self.controller.layer({'layer': 'music', 'action': 'muted', 'value': True})
        self.controller.dispatch('room_volume', {'value': 30})
        self.assertTrue(self.backend.state['music']['muted'])
        self.controller.dispatch('room_volume', {'value': 80})
        self.assertTrue(self.backend.state['music']['muted'])

    def test_relative_changes_clamp_and_bad_values_are_refused(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 96})
        self.controller.dispatch('room_volume', {'delta': 10})
        self.assertEqual(self.controller.room_volume, 100)
        self.controller.dispatch('room_volume', {'delta': -150})
        self.assertEqual(self.controller.room_volume, 0)
        for bad in ({'value': 101}, {'value': -1}, {'value': 'loud'}, {'value': True}, {'delta': 'x'}, {}):
            with self.assertRaises(sessions.SessionError):
                self.controller.dispatch('room_volume', bad)

    def test_saved_rooms_store_it_and_restore_it(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 50})
        self.controller.save({'name': 'Quiet evening'})
        saved = self.controller.variants()[0]
        self.assertEqual(saved['room_volume'], 50)
        self.assertEqual((saved['music']['volume'], saved['atmosphere']['volume']), (50, 20))
        self.controller.dispatch('room_volume', {'value': 100})
        self.controller.enter({'variant_id': saved['id']})
        self.settle()
        self.assertEqual(self.controller.room_volume, 50)
        self.assertEqual(self.backend.state['music']['volume'], 25)
        self.assertEqual(self.backend.state['atmosphere']['volume'], 10)

    def test_a_catalogue_room_keeps_the_session_multiplier(self):
        self.controller.dispatch('room_volume', {'value': 60})
        self.controller.enter({'room_id': 'library'})
        self.settle()
        self.assertEqual(self.controller.room_volume, 60)
        self.assertEqual(self.backend.state['music']['volume'], 30)

    def test_it_survives_a_restart_without_starting_anything(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 35})
        calls = len(self.backend.calls)
        restarted = self.make_controller()
        self.assertEqual(restarted.room_volume, 35)
        self.assertEqual(restarted.snapshot()['room_volume'], 35)
        self.assertEqual(len(self.backend.calls), calls)

    def test_changing_it_never_touches_epochs_or_a_pending_resume(self):
        self.enter()
        self.controller.quiet(10)
        generation, offer, cancel = self.controller.generation, dict(self.controller.resume_offer), self.controller.cancel
        self.controller.dispatch('room_volume', {'value': 20})
        self.assertEqual(self.controller.generation, generation)
        self.assertIs(self.controller.cancel, cancel)
        self.assertFalse(cancel.is_set())
        self.assertEqual(self.controller.resume_offer, offer)
        self.assertEqual(self.backend.state['music']['status'], 'Paused')
        self.assertNotIn(('start_music', 'piano'), self.backend.calls[-2:])

    def test_it_applies_to_a_layer_started_after_it_was_set(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 50})
        self.controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain', 'volume': 30})
        started = [call for call in self.backend.calls if call[0] == 'start_atmosphere'][-1][1]
        self.assertEqual(started['volume'], 15)
        self.assertEqual(self.controller.active['atmosphere']['volume'], 30)

    def test_a_call_duck_follows_the_scaled_level_and_restores_it(self):
        self.enter()
        self.controller.dispatch('room_volume', {'value': 50})
        self.controller.settings['duck_on_call'] = True
        import audio_devices
        with mock.patch.object(audio_devices, 'poll_policy', return_value={'call_active': True, 'duck': True}):
            self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 5)
        with mock.patch.object(audio_devices, 'poll_policy', return_value={'call_active': False}):
            self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 25)

    def test_mpris_volume_is_the_room_volume_and_sets_it(self):
        snapshot = {'layers': {'music': {'status': 'Playing', 'source_id': 'piano', 'volume': 12}}, 'room_volume': 70}
        self.assertEqual(session_service.mpris_values(snapshot)['Volume'], .7)
        snapshot.pop('room_volume')
        self.assertEqual(session_service.mpris_values(snapshot)['Volume'], .12)
        self.enter()
        service = session_service.Service(self.controller)
        service.execute('room_volume', {'value': 25})
        self.assertEqual(session_service.mpris_values(self.controller.snapshot())['Volume'], .25)

    def test_roomctl_reads_and_sets_the_same_value(self):
        sent = []

        def fake(action, args=None, *rest, **keywords):
            sent.append((action, args))
            return {'room_volume': 80}
        with mock.patch.object(session_client, 'request', side_effect=fake), \
             mock.patch.object(sys, 'argv', ['listening-roomctl', 'volume', '+5']):
            self.assertEqual(roomctl.main(), 0)
        self.assertEqual(sent[-1], ('room_volume', {'value': 85}))
        with mock.patch.object(session_client, 'request', side_effect=fake), \
             mock.patch.object(sys, 'argv', ['listening-roomctl', 'volume', '30']):
            self.assertEqual(roomctl.main(), 0)
        self.assertEqual(sent[-1], ('room_volume', {'value': 30}))
        with mock.patch.object(session_client, 'request', side_effect=fake), \
             mock.patch.object(sys, 'argv', ['listening-roomctl', 'volume']), \
             mock.patch('builtins.print') as shown:
            self.assertEqual(roomctl.main(), 0)
        shown.assert_called_with(80)

    def test_timers_are_reported_with_a_controller_clock(self):
        self.enter()
        self.controller.quiet(10)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['timers'], {'break': 1000. + 600})
        self.assertEqual(snapshot['now'], 1000.)
        self.controller.resume()
        self.controller._fade(1800)
        self.assertEqual(self.controller.snapshot()['timers'], {'wind_down': 1000. + 1800})
        self.controller._invalidate()
        self.assertEqual(self.controller.snapshot()['timers'], {})


if __name__ == '__main__':
    unittest.main()
