"""Glance and finish rules: launch arguments, room actions shared by Now, the mini player and the
Waybar popover, feedback capability, and the other-players filter. No GTK display is needed."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_model as model
from next_actions import RoomActions
from test_listening_next_model import snapshot


class Recorder:
    def __init__(self, current):
        self.current, self.sent, self.toasts = current, [], []
        self.actions = RoomActions(self.send, lambda *a: self.toasts.append(a), lambda: self.current)

    def send(self, action, args=None, done=None, urgent=False, quiet=False):
        self.sent.append((action, args or {}, urgent))
        if done:
            done({})


class LaunchArguments(unittest.TestCase):
    def setUp(self):
        import next_window
        self.parse = next_window.launch_arguments

    def test_desk_is_the_old_name_of_the_mini_player(self):
        self.assertEqual(self.parse(['--desk']), ('mini', None))
        self.assertEqual(self.parse(['--mini']), ('mini', None))

    def test_inside_and_browse(self):
        self.assertEqual(self.parse(['--inside']), ('inside', None))
        self.assertEqual(self.parse([]), ('browse', None))

    def test_ui_next_is_accepted_and_classic_is_gone(self):
        self.assertEqual(self.parse(['--ui=next']), ('browse', None))
        with self.assertRaises(ValueError):
            self.parse(['--ui=classic'])

    def test_links(self):
        self.assertEqual(self.parse(['--preview-link', 'https://a.example/x'])[1], 'https://a.example/x')
        self.assertEqual(self.parse(['listening-room://open?url=https%3A%2F%2Fa.example%2Fx'])[1], 'https://a.example/x')
        self.assertEqual(self.parse(['https://a.example/y'])[1], 'https://a.example/y')


class RoomActionsTests(unittest.TestCase):
    def test_pause_is_quiet_and_play_is_resume(self):
        playing = Recorder(snapshot())
        playing.actions.toggle()
        self.assertEqual(playing.sent, [('quiet', {}, True)])
        paused = Recorder(snapshot(resume_offer={'ready': True}, layers={'music': {'status': 'Paused'}}))
        paused.actions.toggle()
        self.assertEqual(paused.sent[0][0], 'resume')

    def test_nothing_without_a_room(self):
        idle = Recorder(snapshot(active=None))
        idle.actions.toggle()
        self.assertEqual(idle.sent, [])

    def test_timers_use_the_existing_commands_with_undo(self):
        r = Recorder(snapshot())
        r.actions.timer('break')
        r.actions.timer('wind_down')
        self.assertEqual([s[:2] for s in r.sent], [('quiet', {'minutes': 10}), ('wind_down', {'minutes': 30})])
        self.assertTrue(all(t[1] == 'Undo' for t in r.toasts))

    def test_leave_names_the_room_and_offers_return(self):
        r = Recorder(snapshot())
        r.actions.leave()
        self.assertEqual(r.sent[0][:3], ('leave', {}, True))
        self.assertEqual(r.toasts[0][:2], ('Left Midnight library', 'Return'))


class Capabilities(unittest.TestCase):
    def test_feedback_only_for_a_generated_station_that_has_played(self):
        self.assertFalse(model.can_feedback(snapshot()))
        station = {'definition': {}, 'upcoming': [], 'recent': [{'name': 'x'}], 'explanation': [], 'can_next': True}
        self.assertTrue(model.can_feedback(snapshot(station=station)))
        self.assertFalse(model.can_feedback(snapshot(station=dict(station, recent=[]))))
        self.assertFalse(model.can_feedback(snapshot(station={'definition': {}, 'upcoming': [], 'recent': [], 'current': None})))
        self.assertTrue(model.now_model(snapshot(station=station))['can_feedback'])

    def test_other_players_exclude_the_rooms_own_layers(self):
        import next_players
        players = [{'id': 'firefox.instance1'}, {'id': 'radio'}, {'id': 'video'}, {'id': 'feishin'}]
        self.assertEqual([p['id'] for p in next_players.external(players)], ['firefox.instance1', 'feishin'])
        self.assertEqual(next_players.player_name('firefox.instance1'), 'Firefox.instance1')
        self.assertEqual(next_players.player_name('feishin'), 'Jellyfin Music')


class ParityPaths(unittest.TestCase):
    def test_world_entry_reads_string_tags_and_falls_back_to_countrycode(self):
        try:
            from unittest.mock import Mock
            from next_listen import ListenPage
        except (ImportError, ValueError):
            self.skipTest('GTK4 bindings unavailable')
        page = Mock(favorites=set())
        entry = ListenPage.world_entry(page, {'name': 'Jazz FM', 'country': 'Finland', 'language': 'Finnish',
                                              'tags': 'jazz, ambient', 'url': 'https://a.example/live'})
        self.assertEqual(entry['detail'], 'Finland · Finnish · jazz, ambient')
        self.assertEqual(entry['keywords'], 'jazz ambient')
        entry = ListenPage.world_entry(page, {'name': 'X', 'countrycode': 'FI', 'tags': ['rock', ' ', 'pop', 'folk'],
                                              'url': 'https://a.example/x'})
        self.assertEqual(entry['detail'], 'FI · rock, pop')
        self.assertEqual(ListenPage.world_entry(page, {'name': 'Y', 'url': 'https://a.example/y'})['detail'], 'World radio')

    def test_range_is_a_station_mood_that_rewrites_only_its_key(self):
        import next_station as st
        self.assertIn('stay_close', [mood['key'] for mood in st.MOODS])
        station = {'name': 'S', 'preferences': {'energy': 'High'}}
        changed = st.with_choice(station, 'stay_close', 'Explore')
        self.assertEqual(changed['preferences'], {'energy': 'High', 'stay_close': 'Explore'})
        self.assertEqual(st.choices(changed)['stay_close'], 'Explore')
        self.assertEqual(st.choices(station)['stay_close'], 'Any')
        self.assertEqual(st.toast_text('stay_close', 'Explore'), 'Range: Explore')
        self.assertIsNone(st.range_hint(station))
        self.assertIn('genres', st.range_hint(changed))
        self.assertEqual(st.undo_of(station, 'stay_close')(changed)['preferences'], {'energy': 'High'})

    def test_rename_has_its_own_undo(self):
        import next_station as st
        station = {'name': 'Before', 'sources': []}
        renamed = dict(station, name='After')
        self.assertEqual(st.undo_of(station, 'name')(renamed)['name'], 'Before')
        self.assertEqual(st.toast_text('name', 'After'), 'Renamed to After')

    def test_remembered_history_projects_to_artist_and_title(self):
        try:
            import next_listen_more as more
        except (ImportError, ValueError):
            self.skipTest('GTK4 bindings unavailable')
        rows = [{'track_metadata': {'artist_name': 'A', 'track_name': 'T'}}, {'other': 1}, 'junk']
        self.assertEqual(more.history_rows(rows), [{'artist': 'A', 'title': 'T'}])
        self.assertEqual(more.media_title({'artist': 'A', 'title': 'T'}), 'A — T')
        self.assertEqual(more.media_title({'station': 'Radio'}), 'Radio')


if __name__ == '__main__':
    unittest.main()
