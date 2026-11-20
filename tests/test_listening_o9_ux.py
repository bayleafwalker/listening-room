"""O9-UX rules without GTK: plain-words errors, the Listen-first room, the explicit command ticket,
the truthful Instrumental-only hint, and the controller's additive snapshot fields and placement default."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import listening_hub as hub
import next_actions
import next_model as model
import next_station
import room_session
from test_listening_session import FakeBackend

CATALOGUE = [{'id': 'midnight-library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'view-lib',
              'poster_path': '/art/library.jpg'}]
PRESETS = [{'id': 'view-lib', 'kind': 'local', 'still_url': '/art/library-still.jpg', 'url': '/art/library.mp4'}]


def room(**music):
    return {'id': 'midnight-library', 'room_id': 'midnight-library', 'name': 'Midnight library',
            'music': dict({'source_id': 'piano'}, **music), 'picture': {'preset_id': 'view-lib', 'enabled': True}}


class Errors(unittest.TestCase):
    def test_the_headline_is_plain_and_the_cause_is_kept_for_details(self):
        for raw in ('[Errno 111] Connection refused', 'ValueError: bad frame', 'Traceback (most recent call last)',
                    'mpv exited: /run/user/1000/listening-hub/radio.sock', 'x' * 300):
            headline = model.plain_error(raw)
            self.assertNotIn('Errno', headline)
            self.assertNotIn('Error:', headline)
            self.assertNotIn('/run/', headline)
            self.assertEqual(model.error_details(raw), raw)

    def test_plain_controller_messages_stay_as_they_are_with_no_details(self):
        text = 'Wait until the current room change finishes'
        self.assertEqual(model.plain_error(text), text)
        self.assertEqual(model.error_details(text), '')

    def test_hyprland_is_detected_from_the_session(self):
        self.assertTrue(model.hyprland({'HYPRLAND_INSTANCE_SIGNATURE': 'abc'}))
        self.assertFalse(model.hyprland({}))


class ListenFirst(unittest.TestCase):
    def test_tune_in_over_a_catalogue_room_marks_it_changed(self):
        self.assertFalse(model.room_changed({'active': room()}, CATALOGUE))
        self.assertTrue(model.room_changed({'active': room(source_id='jazz')}, CATALOGUE))
        station = room(station={'id': 's1', 'name': 'Morning lift'})
        self.assertTrue(model.room_changed({'active': station}, CATALOGUE))

    def test_a_saved_room_is_compared_with_what_was_saved(self):
        saved = dict(room(source_id='jazz'), id='variant-1', name='My jazz room')
        snapshot = {'active': saved, 'variants': [saved]}
        self.assertFalse(model.room_changed(snapshot, CATALOGUE))
        snapshot['active'] = dict(saved, music={'reference': {'provider': 'jellyfin', 'id': 'album-1'}})
        self.assertTrue(model.room_changed(snapshot, CATALOGUE))

    def test_an_ad_hoc_room_is_never_changed_and_is_named_after_what_plays(self):
        active = {'id': 'custom', 'room_id': None, 'name': 'Your listening room',
                  'music': {'source_id': 'groove', 'name': 'Groove Salad'}, 'picture': {'enabled': False}}
        snapshot = {'active': active}
        self.assertTrue(model.ad_hoc(snapshot, CATALOGUE))
        self.assertFalse(model.room_changed(snapshot, CATALOGUE))
        self.assertEqual(model.room_title(snapshot), 'Groove Salad')
        self.assertFalse(model.ad_hoc({'active': room()}, CATALOGUE))

    def test_a_room_saved_under_a_generic_name_is_titled_and_saved_by_what_plays(self):
        saved = {'id': 'variant-your', 'room_id': None, 'name': 'Your listening room',
                 'music': {'source_id': 'groove', 'name': 'Groove Salad'}}
        snapshot = {'active': saved, 'variants': [saved]}
        self.assertFalse(model.ad_hoc(snapshot, CATALOGUE))
        self.assertEqual(model.shown_name(snapshot, CATALOGUE), 'Groove Salad')
        self.assertEqual(model.suggested_name(snapshot), 'Groove Salad')
        named = dict(saved, name='Rainy reading')
        self.assertEqual(model.shown_name({'active': named, 'variants': [named]}, CATALOGUE), 'Rainy reading')
        self.assertEqual(model.suggested_name({'active': named}), 'Rainy reading')

    def test_a_room_without_a_scene_falls_back_to_the_last_scene_then_a_default(self):
        last = room()
        assets = model.fallback_scene({'last_session': last}, PRESETS, CATALOGUE)
        self.assertEqual(assets['still'], '/art/library-still.jpg')
        self.assertIsNone(assets['loop'])
        self.assertEqual(model.fallback_scene({}, PRESETS, CATALOGUE)['still'], '/art/library-still.jpg')
        self.assertIsNone(model.fallback_scene({}, [], [])['still'])


class Tickets(unittest.TestCase):
    def test_finish_runs_each_callback_once_even_when_added_late(self):
        ticket, seen = next_actions.Ticket(), []
        ticket.add_finish(lambda: seen.append('early'))
        ticket.finish()
        ticket.finish()
        ticket.add_finish(lambda: seen.append('late'))
        self.assertEqual(seen, ['early', 'late'])

    def test_nothing_sent_restores_at_once(self):
        seen = []
        next_actions.when_finished(None, lambda: seen.append(1))
        next_actions.when_finished(False, lambda: seen.append(2))
        self.assertEqual(seen, [1, 2])

    def test_room_actions_hand_back_what_send_returned(self):
        ticket = next_actions.Ticket()
        actions = next_actions.RoomActions(lambda *a, **k: ticket, lambda *a, **k: None,
                                           lambda: {'active': room(), 'layers': {'music': {'status': 'Playing'}}})
        self.assertIs(actions.toggle(), ticket)
        self.assertIsNone(next_actions.RoomActions(lambda *a, **k: ticket, None, lambda: {}).toggle())


class StationHint(unittest.TestCase):
    def test_the_instrumental_only_hint_points_somewhere_that_can_review(self):
        station = {'preferences': {'speech': 'Strict instrumental'}}
        for count in (0, 3):
            text = next_station.hints(station, count)
            self.assertNotIn('Preferences', text)
            self.assertIn('Track details', text)
        self.assertTrue(next_station.wants_review(station))
        self.assertFalse(next_station.wants_review({'preferences': {'speech': 'Any'}}))


class Controller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(base / 'config'), 'XDG_STATE_HOME': str(base / 'state')})
        patcher.start()
        self.addCleanup(patcher.stop)
        state = mock.patch.object(hub, 'STATE', base / 'hub-state')
        state.start()
        self.addCleanup(state.stop)
        self.directory = base / 'controller'

    def controller(self):
        controller = room_session.Controller(FakeBackend(), self.directory)
        self.addCleanup(controller.close)
        return controller

    def test_the_default_placement_is_here_and_an_explicit_choice_is_kept(self):
        controller = self.controller()
        self.assertEqual(controller.snapshot()['settings']['placement'], 'here')
        controller.apply_settings({'placement': '9'})
        controller.close()
        self.assertEqual(self.controller().snapshot()['settings']['placement'], '9')

    def test_an_old_stored_default_of_workspace_9_moves_to_here(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'session.json').write_text(json.dumps({'version': 1, 'settings': {'placement': '9', 'still': True}}))
        settings = self.controller().snapshot()['settings']
        self.assertEqual((settings['placement'], settings['still']), ('here', True))

    def test_recipes_recorded_under_the_old_default_open_here_once(self):
        self.directory.mkdir(parents=True)
        last = {'id': 'custom', 'name': 'Late room', 'picture': {'enabled': True, 'placement': '9'}}
        variant = {'id': 'variant-a', 'name': 'A', 'music': {}, 'picture': {'enabled': True, 'placement': '9'}}
        (self.directory / 'session.json').write_text(json.dumps({'version': 1, 'last_session': last,
                                                                 'settings': {'placement': '9'}}))
        (self.directory / 'room-variants.json').write_text(json.dumps([variant]))
        controller = self.controller()
        self.assertEqual(controller.last_session['picture']['placement'], 'here')
        self.assertEqual(controller.variants()[0]['picture']['placement'], 'here')
        self.assertNotIn('placement_chosen', controller.snapshot()['settings'])
        # Once: a room placed on workspace 9 afterwards keeps it.
        variants = json.loads((self.directory / 'room-variants.json').read_text())
        variants[0]['picture']['placement'] = '9'
        (self.directory / 'room-variants.json').write_text(json.dumps(variants))
        controller.close()
        self.assertEqual(self.controller().variants()[0]['picture']['placement'], '9')

    def test_an_explicit_choice_keeps_the_recipes_as_they_are(self):
        self.directory.mkdir(parents=True)
        last = {'id': 'custom', 'name': 'Late room', 'picture': {'enabled': True, 'placement': '9'}}
        (self.directory / 'session.json').write_text(json.dumps({'version': 1, 'last_session': last,
                                                                 'settings': {'placement': '9', 'placement_chosen': True}}))
        controller = self.controller()
        self.assertEqual((controller.settings['placement'], controller.last_session['picture']['placement']), ('9', '9'))

    def test_the_config_generation_changes_when_a_library_or_favourite_is_saved(self):
        controller = self.controller()
        first = controller.snapshot()['config_generation']
        self.assertEqual(controller.snapshot()['config_generation'], first)
        connections = Path(os.environ['XDG_CONFIG_HOME']) / 'listening-hub' / 'connections.json'
        connections.parent.mkdir(parents=True)
        connections.write_text('{}')
        second = controller.snapshot()['config_generation']
        self.assertNotEqual(second, first)
        hub.save_favorite({'url': 'https://radio.example.test/a', 'name': 'A'})
        self.assertNotEqual(controller.snapshot()['config_generation'], second)

    def test_station_learned_says_whether_there_is_anything_to_forget(self):
        controller = self.controller()
        controller.station_memory = {'s1': {'liked_artists': ['A'], 'disliked': []}, 's2': {'reset_at': 5}}
        self.assertEqual(controller.snapshot()['station_learned'], {'s1': True, 's2': False})


if __name__ == '__main__':
    unittest.main()
