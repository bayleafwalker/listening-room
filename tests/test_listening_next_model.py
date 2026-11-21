"""Launch rules, vocabulary, capabilities and wording for the next Listening Room window."""
import copy
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import next_model as model

FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh')


def snapshot(**changes):
    value = {'active': {'id': 'midnight-library', 'room_id': 'midnight-library', 'name': 'Midnight library',
                        'accent': '#c79b68', 'poster_path': '/art/midnight.jpg',
                        'music': {'source_id': 'piano', 'name': 'Dark academia piano', 'volume': 50, 'muted': False},
                        'atmosphere': {'kind': 'local', 'source_id': 'rain-glass', 'name': 'Rain on glass', 'volume': 20,
                                       'outdoor': True, 'window_open': 35},
                        'picture': {'preset_id': 'room-midnight-library', 'enabled': True, 'placement': '9'}},
             'last_session': None, 'variants': [], 'settings': {}, 'errors': {}, 'timers': {}, 'now': 1000.,
             'room_volume': 100, 'levels': {'music': 50, 'atmosphere': 20}, 'resume_offer': None,
             'layers': {'music': {'status': 'Playing', 'source_id': 'piano', 'name': 'Dark academia piano',
                                  'title': 'Nocturne', 'volume': 50, 'muted': False},
                        'atmosphere': {'status': 'Playing', 'kind': 'local', 'name': 'Rain on glass', 'outdoor': True,
                                       'volume': 20, 'muted': False, 'window_open': 35},
                        'picture': {'status': 'Playing', 'name': 'Midnight Library', 'placement': '9'}}}
    value.update(changes)
    return value


def library_queue():
    queue = [{'provider': 'jellyfin', 'id': 'a', 'type': 'Audio', 'name': 'First'},
             {'provider': 'jellyfin', 'id': 'b', 'type': 'Audio', 'name': 'Second'}]
    value = snapshot()
    value['active']['music'] = {'source_id': 'jellyfin-a', 'name': 'First', 'reference': queue[0], 'queue': queue,
                                'queue_index': 0, 'volume': 50, 'muted': False}
    value['layers']['music'] = {'status': 'Playing', 'source_id': 'jellyfin-a', 'title': 'First', 'artist': 'Someone',
                                'volume': 50, 'muted': False}
    return value


class SnapshotChangeTests(unittest.TestCase):
    def test_only_keys_that_differ_count_and_the_clock_only_by_the_minute(self):
        old = {'now': 600.0, 'active': {'id': 'a'}, 'stations': [{'id': 's'}], 'room_volume': 80}
        self.assertIsNone(model.snapshot_changes(None, old))
        self.assertIsNone(model.snapshot_changes(old, old), 'the same snapshot again draws everything')
        same = copy.deepcopy(old)
        same['now'] = 601.0
        self.assertEqual(model.snapshot_changes(old, same), set())
        moved = dict(same, room_volume=55, now=661.0)
        self.assertEqual(model.snapshot_changes(old, moved), {'room_volume', 'minute'})
        gone = {key: value for key, value in old.items() if key != 'stations'}
        self.assertEqual(model.snapshot_changes(old, gone), {'stations'})

    def test_now_sees_a_variants_change_only_for_the_room_it_shows(self):
        old = {'active': {'id': 'v1'}, 'variants': [{'id': 'v1', 'name': 'Den'}, {'id': 'v2', 'name': 'Study'}]}
        other = copy.deepcopy(old)
        other['variants'][1]['name'] = 'Library'
        self.assertEqual(model.now_changes({'variants', 'rooms'}, old, other), {'rooms'})
        own = copy.deepcopy(old)
        own['variants'][0]['name'] = 'Den two'
        self.assertEqual(model.now_changes({'variants'}, old, own), {'variants'})
        self.assertIsNone(model.now_changes(None, old, other))

    def test_touches(self):
        self.assertTrue(model.touches(None, {'a'}))
        self.assertFalse(model.touches(set(), None))
        self.assertTrue(model.touches({'b', 'a'}, {'a'}))
        self.assertFalse(model.touches({'stations'}, ignores=model.NOW_IGNORES))
        self.assertTrue(model.touches({'stations', 'layers'}, ignores=model.NOW_IGNORES))


class NowTextTests(unittest.TestCase):
    def snapshot(self, title='', artist=''):
        return {'active': {'id': 'adhoc', 'name': 'Your listening room', 'music': {'name': 'Groove Radio'}},
                'layers': {'music': {'status': 'Playing', 'station': 'Groove Radio', 'title': title, 'artist': artist}}}

    def test_a_room_named_after_its_station_says_the_name_once(self):
        self.assertEqual(model.now_text(self.snapshot()), ('Groove Radio', '', ''))
        self.assertEqual(model.now_text(self.snapshot('Night Drive')), ('Groove Radio', 'Night Drive', ''))
        self.assertEqual(model.now_text(self.snapshot('Night Drive', 'Someone')), ('Groove Radio', 'Night Drive', 'Someone'))

    def test_the_mix_button_and_mini_players_do_not_repeat_it_either(self):
        snap = self.snapshot()
        self.assertEqual(model.layer_summary(snap, 'Groove Radio'), 'Music')
        self.assertEqual(model.layer_summary(snap), 'Groove Radio')
        self.assertEqual(model.layer_summary(snap, 'Midnight library'), 'Groove Radio')
        self.assertEqual(model.without_name('Groove Radio', 'groove radio ', 'Night Drive'), ('Night Drive', ''))
        self.assertEqual(model.without_name('Groove Radio', 'Night Drive', 'Groove Radio'), ('Night Drive', ''))
        self.assertEqual(model.without_name('Groove Radio', 'Night Drive', 'Someone'), ('Night Drive', 'Someone'))


class LaunchTests(unittest.TestCase):
    def test_an_active_room_opens_now_otherwise_rooms(self):
        self.assertEqual(model.launch_view(snapshot()), 'now')
        self.assertEqual(model.launch_view(snapshot(active=None)), 'rooms')
        self.assertEqual(model.launch_view({}), 'rooms')

    def test_return_hero_names_the_last_room_and_first_run_offers_no_button(self):
        last = copy.deepcopy(snapshot()['active'])
        hero = model.hero(snapshot(active=None, last_session=last, last_entered_at=900.), [{'id': 'midnight-library', 'subtitle': 'Piano by lamplight'}])
        self.assertEqual(hero['kind'], 'return')
        self.assertEqual(hero['button'], 'Return to Midnight library')
        self.assertEqual(hero['detail'], 'Piano by lamplight, with Rain on glass')
        self.assertEqual(model.hero(snapshot(active=None))['kind'], 'first-run')
        self.assertEqual(model.hero(snapshot())['kind'], 'now')

    def test_when_words_are_relative_and_never_crash_on_missing_data(self):
        self.assertEqual(model.when_words(None, 10), 'Last time')
        self.assertEqual(model.when_words(0, 86400 * 1.5), 'Yesterday')
        self.assertEqual(model.when_words(0, 3600 * 8), 'Earlier today')


class NowTests(unittest.TestCase):
    def test_live_radio_never_offers_next_track(self):
        info = model.now_model(snapshot())
        self.assertFalse(info['can_next'])
        self.assertEqual(info['chip'], 'Live radio')
        self.assertEqual(info['name'], 'Midnight library')
        self.assertEqual(info['summary'], 'Dark academia piano · Rain on glass · Scene')

    def test_a_library_queue_with_a_following_track_offers_next_track(self):
        info = model.now_model(library_queue())
        self.assertTrue(info['can_next'])
        self.assertEqual(info['chip'], 'Library')
        self.assertEqual((info['line'], info['detail']), ('First', 'Someone'))
        last = library_queue()
        last['active']['music']['queue_index'] = 1
        self.assertFalse(model.now_model(last)['can_next'])

    def test_play_pause_state_follows_the_resume_offer(self):
        self.assertEqual(model.room_state(snapshot()), 'playing')
        # Room Pause (Quiet) pauses every playing layer and offers them back.
        paused = snapshot(resume_offer={'layers': ['music', 'atmosphere'], 'ready': False})
        paused['layers']['music']['status'] = 'Paused'
        paused['layers']['atmosphere']['status'] = 'Paused'
        self.assertEqual(model.room_state(paused), 'paused')
        self.assertEqual(model.room_state(snapshot(layers={})), 'idle')

    def test_room_shows_pause_while_anything_is_audible(self):
        # O10 / Proposal E: one layer paused in the Mix leaves the room button on Pause, matching MPRIS.
        partly = snapshot()
        partly['layers']['music']['status'] = 'Paused'
        self.assertEqual(model.room_state(partly), 'playing')
        partly['layers']['atmosphere']['status'] = 'Paused'
        self.assertEqual(model.room_state(partly), 'paused', 'every layer paused on its own: Play resumes them')

    def test_start_outcome_reads_the_controllers_entry(self):
        pending = {'id': 7}
        self.assertIsNone(model.start_outcome(snapshot(transition={'kind': 'enter'}, entry={'id': 7, 'state': 'connecting'}), pending))
        self.assertEqual(model.start_outcome(snapshot(entry={'id': 7, 'state': 'connecting'}), pending), ('started', None),
                         'interrupted by Quiet or Stop: the card clears')
        self.assertIsNone(model.start_outcome(snapshot(transition={'kind': 'enter'}, entry={'id': 7, 'state': 'failed'}), pending))
        self.assertIsNone(model.start_outcome(snapshot(entry={'id': 6, 'state': 'started'}), pending), 'an older start')
        self.assertEqual(model.start_outcome(snapshot(entry={'id': 7, 'state': 'failed', 'error': 'refused'}), pending),
                         ('failed', 'refused'))
        self.assertEqual(model.start_outcome(snapshot(entry={'id': 7, 'state': 'started'}), pending), ('started', None))
        self.assertEqual(model.start_outcome(snapshot(entry={'id': 9, 'state': 'connecting'}), pending), ('started', None),
                         'superseded by a newer start: not this card')
        queued = {'entry_before': 3}
        self.assertIsNone(model.start_outcome(snapshot(entry={'id': 3, 'state': 'started'}), queued))
        self.assertEqual(model.start_outcome(snapshot(entry={'id': 4, 'state': 'failed', 'error': 'x'}), queued), ('failed', 'x'))
        self.assertEqual(model.start_error('Connection refused'), 'The music could not connect.')

    def test_another_rooms_failed_start_is_not_this_rooms_music_failure(self):
        kept = snapshot(errors={'music': 'refused'},
                        entry={'id': 2, 'state': 'failed', 'error': 'refused', 'name': 'Northern quiet', 'kept': 'Midnight library'})
        self.assertIsNone(model.failure(kept))
        own = snapshot(errors={'music': 'refused'},
                       entry={'id': 2, 'state': 'failed', 'error': 'refused', 'name': 'Midnight library', 'kept': 'Midnight library'})
        self.assertEqual(model.failure(own)['layer'], 'music', 'a retry of this room\'s own music shows on its layer')

    def test_no_room_means_no_now(self):
        self.assertIsNone(model.now_model(snapshot(active=None)))


class FailureTests(unittest.TestCase):
    def test_one_banner_names_the_layer_and_what_keeps_playing(self):
        failure = model.failure(snapshot(errors={'atmosphere': 'No such sound'}))
        self.assertEqual(failure['layer'], 'atmosphere')
        self.assertEqual(failure['title'], 'Ambience couldn’t start')
        self.assertEqual(failure['detail'], 'Music and Scene keep playing.')

    def test_wording_adapts_to_how_many_layers_are_left(self):
        broken = snapshot(errors={'music': 'x'})
        broken['layers']['atmosphere']['status'] = 'Stopped'
        self.assertEqual(model.failure(broken)['detail'], 'Scene keeps playing.')
        broken['layers']['picture']['status'] = 'Stopped'
        self.assertEqual(model.failure(broken)['detail'], 'Nothing else is playing.')

    def test_only_the_highest_priority_layer_gets_the_banner_and_session_errors_do_not(self):
        both = snapshot(errors={'picture': 'p', 'music': 'm'})
        self.assertEqual(model.failure(both)['layer'], 'music')
        self.assertIsNone(model.failure(snapshot(errors={'refresh': 'x', 'output': 'y'})))


class TimerTests(unittest.TestCase):
    def test_remaining_minutes_round_up_and_finished_timers_disappear(self):
        running = model.timers(snapshot(timers={'wind_down': 1000 + 24 * 60 - 20, 'break': 1000 + 90, 'dim': 999}))
        self.assertEqual([(t['kind'], t['minutes']) for t in running], [('break', 2), ('wind_down', 24)])
        self.assertEqual(running[1]['label'], 'Wind down · 24 min')
        self.assertEqual(running[0]['label'], 'Break · 2 min')
        self.assertEqual(model.timers({'timers': {'break': 5}}), [])


class MixTests(unittest.TestCase):
    def rows(self, value=None):
        return {row['layer']: row for row in model.mix_rows(value or snapshot())}

    def test_three_rows_in_listener_words(self):
        self.assertEqual([r['title'] for r in model.mix_rows(snapshot())], ['Music', 'Ambience', 'Scene'])

    def test_window_exists_only_for_outdoor_local_sounds(self):
        self.assertTrue(self.rows()['atmosphere']['outdoor'])
        indoor = snapshot()
        indoor['layers']['atmosphere'].update(name='Fireplace', outdoor=False)
        indoor['active']['atmosphere'].update(outdoor=False)
        self.assertFalse(self.rows(indoor)['atmosphere']['outdoor'])

    def test_levels_come_from_layer_levels_not_the_scaled_player(self):
        scaled = snapshot(room_volume=40, levels={'music': 50, 'atmosphere': 20})
        scaled['layers']['music']['volume'] = 20
        self.assertEqual(self.rows(scaled)['music']['level'], 50)

    def test_scene_row_has_placement_and_moving_picture(self):
        row = self.rows()['picture']
        self.assertEqual((row['placement'], row['moving']), ('9', True))
        still = snapshot()
        still['active']['picture']['still'] = True
        self.assertFalse(self.rows(still)['picture']['moving'])
        self.assertEqual([name for _key, name in model.PLACEMENTS], ['Workspace 9', 'Here', 'On top', 'Small'])

    def test_next_track_follows_each_layers_ability(self):
        self.assertFalse(self.rows()['music']['can_next'])
        self.assertTrue(self.rows(library_queue())['music']['can_next'])
        queued = snapshot()
        queued['layers']['atmosphere']['can_next'] = True
        self.assertTrue(self.rows(queued)['atmosphere']['can_next'])

    def test_a_failed_layer_says_so_and_a_closed_scene_reads_closed(self):
        failed = snapshot(errors={'atmosphere': 'x'})
        self.assertTrue(self.rows(failed)['atmosphere']['failed'])
        closed = snapshot()
        closed['layers']['picture']['status'] = 'Stopped'
        self.assertEqual(self.rows(closed)['picture']['state'], 'closed')


class SceneTests(unittest.TestCase):
    PRESETS = [{'id': 'room-midnight-library', 'kind': 'local', 'url': '/assets/room-midnight-library.mp4',
                'still_url': '/assets/room-midnight-library-still.jpg', 'poster_path': '/assets/room-midnight-library.png'}]

    def test_assets_use_the_rooms_reviewed_still_and_loop(self):
        assets = model.scene_assets(snapshot()['active'], self.PRESETS)
        self.assertEqual(assets['still'], '/assets/room-midnight-library-still.jpg')
        self.assertEqual(assets['loop'], '/assets/room-midnight-library.mp4')

    def test_a_room_without_a_loop_still_has_art(self):
        assets = model.scene_assets(snapshot()['active'], [])
        self.assertIsNone(assets['loop'])
        self.assertEqual(assets['still'], '/art/midnight.jpg')

    def test_motion_is_off_for_reduced_motion_still_preference_or_disabled_animations(self):
        self.assertTrue(model.motion_allowed(snapshot()))
        self.assertFalse(model.motion_allowed(snapshot(settings={'reduced_motion': True})))
        self.assertFalse(model.motion_allowed(snapshot(settings={'still': True})))
        self.assertFalse(model.motion_allowed(snapshot(), animations_enabled=False))


class VocabularyTests(unittest.TestCase):
    def strings(self, value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from self.strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from self.strings(item)

    def test_nothing_the_model_says_uses_engine_words(self):
        states = [snapshot(), library_queue(), snapshot(errors={'atmosphere': 'x'}),
                  snapshot(errors={'picture': 'x'}), snapshot(timers={'wind_down': 1600, 'dim': 1700, 'break': 1200}),
                  snapshot(active=None, last_session=snapshot()['active'])]
        for value in states:
            produced = [model.now_model(value), model.mix_rows(value), model.hero(value), model.timers(value),
                        list(model.LAYER_WORDS.values()), [name for _key, name in model.PLACEMENTS]]
            for text in self.strings(produced):
                for word in FORBIDDEN:
                    self.assertNotIn(word, text)

    def test_layer_words(self):
        self.assertEqual(model.LAYER_WORDS, {'music': 'Music', 'atmosphere': 'Ambience', 'picture': 'Scene'})


class LaunchOptionTests(unittest.TestCase):
    def test_classic_is_gone_and_desk_opens_the_mini_player(self):
        import listening_room
        import next_window
        self.assertFalse(listening_room.classic_requested([]))
        self.assertFalse(listening_room.classic_requested(['--desk', '--ui=next']))
        self.assertTrue(listening_room.classic_requested(['--ui=classic']))
        self.assertTrue(listening_room.classic_requested(['--ui', 'classic']))
        self.assertEqual(next_window.launch_arguments(['--desk']), ('mini', None))
        self.assertEqual(next_window.launch_arguments([]), ('browse', None))
        with self.assertRaises(ValueError):
            next_window.launch_arguments(['--ui=classic'])

    def test_the_model_needs_no_toolkit(self):
        self.assertFalse(hasattr(model, 'gi') or hasattr(model, 'Gtk'))


class StyleTests(unittest.TestCase):
    def test_the_stylesheet_parses_without_errors(self):
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        import next_widgets
        provider = Gtk.CssProvider()
        problems = []
        provider.connect('parsing-error', lambda _p, section, error: problems.append(error.message))
        provider.load_from_string(next_widgets.CSS)
        provider.load_from_string(next_widgets.accent_css('#76a9bd'))
        self.assertEqual(problems, [])

    def test_accent_text_uses_a_light_tint_that_reads_on_dark(self):
        import next_widgets
        for accent in ('#c79b68', '#76a9bd', '#9cb8b8', '#d6b671', '#c5a5df', '#e4a767', '#a9b7a2', '#b6ad85', 'not a colour'):
            tint = next_widgets.tint(accent)
            channels = [int(tint[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
            luminance = .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
            window = .0086  # relative luminance of the #17171a window
            self.assertGreaterEqual((luminance + .05) / (window + .05), 4.5, tint)


if __name__ == '__main__':
    unittest.main()
