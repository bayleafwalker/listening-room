"""Station vocabulary, mood mapping and link helpers for the Listen surface."""
import datetime
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import next_station as station


def library(**changes):
    value = {
        'id': 'station-late-reading', 'name': 'Late reading',
        'sources': [{'reference': {'provider': 'jellyfin', 'id': 'album-1', 'type': 'MusicAlbum',
                                   'name': 'Neoclassical collection', 'genres': ['Neoclassical']}}],
        'fallbacks': [
            {'source_id': 'somafm-drone-zone', 'name': 'SomaFM Drone Zone',
             'url': 'https://somafm.com/dronezone.pls', 'kind': 'radio'},
            {'source_id': 'ylex', 'name': 'YleX', 'url': 'https://icecast.live.yle.fi/radio/YleX/i', 'kind': 'radio'},
        ],
        'preferences': {'energy': 'Low', 'familiarity': 'Any', 'speech': 'Any'},
        'end_policy': 'stop', 'arc': 'wind_down', 'generation': {'kind': 'collection'},
    }
    value.update(changes)
    return value


def radio(**changes):
    value = {
        'id': 'station-morning-lift', 'name': 'Morning lift',
        'sources': [{'source_id': 'somafm-groove-salad', 'name': 'SomaFM Groove Salad',
                     'url': 'https://somafm.com/groovesalad.pls', 'kind': 'radio'}],
        'fallbacks': [], 'preferences': {}, 'end_policy': 'repeat', 'arc': 'none',
        'generation': {'kind': 'collection'},
    }
    value.update(changes)
    return value


def program(**changes):
    value = {'definition': {'id': 'station-late-reading', 'name': 'Late reading'},
             'upcoming': [{'name': 'Quiet study', 'artist': 'Someone Else'},
                          {'name': 'Slow light', 'artist': 'Third'}],
             'recent': [], 'current': {'name': 'Nocturne in C'},
             'explanation': ['Familiarity uses favorites and listening history'], 'hold': True,
             'can_next': True}
    value.update(changes)
    return value


class MoodMappingTests(unittest.TestCase):
    def test_each_three_choice_control_maps_onto_the_existing_values(self):
        self.assertEqual(station.value_for('energy', 'Calm'), 'Low')
        self.assertEqual(station.value_for('energy', 'Any'), 'Any')
        self.assertEqual(station.value_for('energy', 'Lively'), 'High')
        self.assertEqual(station.value_for('familiarity', 'Familiar'), 'Familiar')
        self.assertEqual(station.value_for('familiarity', 'A mix'), 'Any')
        self.assertEqual(station.value_for('familiarity', 'Discover'), 'New')
        self.assertEqual(station.value_for('speech', 'Any'), 'Any')
        self.assertEqual(station.value_for('speech', 'Fewer'), 'Likely instrumental')
        self.assertEqual(station.value_for('speech', 'Instrumental only'), 'Strict instrumental')

    def test_fewer_shows_for_both_folded_values_and_speech_welcome_reads_any(self):
        self.assertEqual(station.choices(library(preferences={'speech': 'Likely instrumental'}))['speech'], 'Fewer')
        self.assertEqual(station.choices(library(preferences={'speech': 'Less speech'}))['speech'], 'Fewer')
        self.assertEqual(station.choices(library(preferences={'speech': 'Speech welcome'}))['speech'], 'Any')
        self.assertEqual(station.choices(library(preferences={}))['speech'], 'Any')
        self.assertEqual(station.choices(library(preferences={'energy': 'High'}))['energy'], 'Lively')
        self.assertEqual(station.choices(library(preferences={'familiarity': 'New'}))['familiarity'], 'Discover')

    def test_one_choice_rewrites_only_that_preference(self):
        changed = station.with_choice(library(), 'speech', 'Instrumental only')
        self.assertEqual(changed['preferences'], {'energy': 'Low', 'familiarity': 'Any', 'speech': 'Strict instrumental'})
        self.assertEqual(changed['fallbacks'], library()['fallbacks'])
        self.assertEqual(station.with_choice(library(), 'energy', 'Lively')['preferences']['energy'], 'High')

    def test_a_folded_value_keeps_its_stored_form_until_another_choice_is_made(self):
        stored = library(preferences={'speech': 'Less speech'})
        self.assertEqual(station.choices(stored)['speech'], 'Fewer')
        self.assertEqual(stored['preferences']['speech'], 'Less speech')
        self.assertEqual(station.with_choice(stored, 'energy', 'Calm')['preferences']['speech'], 'Less speech')
        self.assertEqual(station.with_choice(stored, 'speech', 'Fewer')['preferences']['speech'], 'Likely instrumental')

    def test_arc_and_end_choices_round_trip(self):
        self.assertEqual([name for name, _value in station.ARCS], ['Steady', 'Wind down', 'Rise'])
        self.assertEqual(station.arc_label(library()), 'Wind down')
        self.assertEqual(station.arc_label(station.with_arc(library(), 'Rise')), 'Rise')
        self.assertEqual(station.arc_label(station.with_arc(library(), 'Steady')), 'Steady')
        self.assertEqual(station.end_label(library()), 'Stop')
        self.assertEqual(station.end_label(station.with_end(library(), 'Repeat')), 'Repeat')

    def test_one_hint_appears_only_for_the_constrained_voice_value(self):
        self.assertIsNone(station.hints(library(preferences={'speech': 'Any'})))
        self.assertIsNone(station.hints(library(preferences={'energy': 'High'})))
        self.assertIn('model estimates', station.hints(library(preferences={'speech': 'Likely instrumental'})))
        self.assertIn('spoken word', station.hints(library(preferences={'speech': 'Less speech'})))
        unknown = station.hints(library(preferences={'speech': 'Strict instrumental'}))
        self.assertIn('reviewed', unknown)
        none_yet = station.hints(library(preferences={'speech': 'Strict instrumental'}), 0)
        self.assertIn('No reviewed recordings', none_yet)
        some = station.hints(library(preferences={'speech': 'Strict instrumental'}), 2)
        self.assertIn('2 reviewed recordings available', some)

    def test_confirmations_are_specific(self):
        self.assertEqual(station.toast_text('energy', 'Lively'), 'Energy set to Lively')
        self.assertEqual(station.toast_text('speech', 'Instrumental only'), 'Voices: Instrumental only')
        self.assertEqual(station.toast_text('arc', 'Wind down'), 'Wind down is on for the next track')
        self.assertEqual(station.toast_text('end', 'Repeat'), 'Repeat when it runs out')
        self.assertEqual(station.toast_text('hold', True), 'Holding this mood')
        self.assertEqual(station.toast_text('hold', False), 'Mood can change again')


class DescriptorTests(unittest.TestCase):
    def test_cards_name_the_kind_and_the_most_important_trait(self):
        self.assertEqual(station.descriptor(library()), 'Library · winds down · 2 fallbacks')
        self.assertEqual(station.descriptor(radio()), 'Radio · SomaFM Groove Salad')
        strict = library(preferences={'speech': 'Strict instrumental'}, fallbacks=[])
        self.assertEqual(station.descriptor(strict), 'Library · winds down · instrumental only')

    def test_origin_says_where_the_music_comes_from(self):
        self.assertEqual(station.origin(library()), 'Library station · Neoclassical · Jellyfin')
        self.assertEqual(station.origin(radio()), 'Radio station · SomaFM Groove Salad')

    def test_only_library_stations_carry_learning_and_programming(self):
        self.assertTrue(station.programmable(library()))
        self.assertFalse(station.programmable(radio()))

    def test_initials_and_tints_are_stable(self):
        self.assertEqual(station.initials('Late reading'), 'LR')
        self.assertEqual(station.initials('SomaFM Groove Salad'), 'SG')
        self.assertEqual(station.initials(''), '·')
        self.assertEqual(station.tint_index('Late reading'), station.tint_index('Late reading'))
        self.assertTrue(0 <= station.tint_index('YleX') < station.TINTS)


class FallbackTests(unittest.TestCase):
    def test_fallbacks_keep_their_order_and_say_where_they_come_from(self):
        rows = station.fallbacks(library())
        self.assertEqual([row['name'] for row in rows], ['SomaFM Drone Zone', 'YleX'])
        self.assertEqual([row['index'] for row in rows], [0, 1])
        self.assertEqual(rows[0]['detail'], 'Radio')

    def test_moving_an_item_and_ignoring_impossible_moves(self):
        values = ['a', 'b', 'c']
        self.assertEqual(station.moved(values, 0, 2), ['b', 'c', 'a'])
        self.assertEqual(station.moved(values, 2, 0), ['c', 'a', 'b'])
        self.assertEqual(station.moved(values, 1, 1), values)
        self.assertEqual(station.moved(values, 0, 5), values)
        self.assertEqual(station.moved(values, -1, 1), values)
        self.assertEqual(values, ['a', 'b', 'c'])

    def test_with_fallbacks_keeps_everything_else(self):
        changed = station.with_fallbacks(library(), list(reversed(library()['fallbacks'])))
        self.assertEqual([row['name'] for row in station.fallbacks(changed)], ['YleX', 'SomaFM Drone Zone'])
        self.assertEqual(changed['preferences'], library()['preferences'])
        self.assertEqual(changed['id'], library()['id'])


class HistoryTests(unittest.TestCase):
    NOW = 1_800_000_000.

    def test_replay_needs_captured_tracks(self):
        rows = station.history_rows([
            {'id': 'evening-1', 'station': library(), 'time': self.NOW - 60, 'tracks': [{'id': 'a'}, {'id': 'b'}]},
            {'id': 'evening-2', 'station': radio(), 'time': self.NOW - 60, 'tracks': []},
            {'id': 'evening-3', 'station': radio(), 'time': self.NOW - 60},
        ], self.NOW)
        self.assertEqual([row['action'] for row in rows], ['replay', 'tune', 'tune'])
        self.assertEqual(rows[0]['name'], 'Late reading')
        self.assertEqual(rows[0]['tracks'], 2)
        self.assertIn('2 tracks', rows[0]['detail'])
        self.assertIn('no tracks kept', rows[1]['detail'])
        self.assertEqual(rows[2]['tracks'], 0)

    def test_when_words_are_relative_and_never_crash(self):
        self.assertEqual(station.when_words(None, self.NOW), 'Earlier')
        self.assertEqual(station.when_words(self.NOW + 5, self.NOW), 'Just now')
        self.assertEqual(station.when_words(self.NOW - 60, self.NOW), 'Just now')
        self.assertEqual(station.when_words(self.NOW - 6 * 3600, self.NOW), 'Today')
        self.assertEqual(station.when_words(self.NOW - 30 * 3600, self.NOW), 'Yesterday')
        moment = self.NOW - 3 * 86400
        stamp = datetime.datetime.fromtimestamp(moment)
        part = 'morning' if 5 <= stamp.hour < 12 else 'afternoon' if 12 <= stamp.hour < 17 else 'evening'
        self.assertEqual(station.when_words(moment, self.NOW), stamp.strftime('%A') + ' ' + part)
        self.assertEqual(station.when_words(self.NOW - 60 * 86400, self.NOW), 'A while ago')

    def test_history_ignores_malformed_entries(self):
        self.assertEqual(station.history_rows([None, 'x', {}], self.NOW)[0]['name'], 'Station')


class ProgramTests(unittest.TestCase):
    def test_learning_upcoming_and_hold_come_from_the_running_program(self):
        self.assertEqual(station.learned(program(), library()), ['Familiarity uses favorites and listening history'])
        self.assertEqual([row['name'] for row in station.upcoming(program(), library())],
                         ['Quiet study', 'Slow light'])
        self.assertTrue(station.hold(program(), library()))

    def test_another_station_or_a_radio_definition_learns_nothing(self):
        other = dict(program(), definition={'id': 'station-other'})
        self.assertEqual(station.learned(other, library()), [])
        self.assertEqual(station.upcoming(other, library()), [])
        self.assertIsNone(station.hold(other, library()))
        bare = dict(program())
        bare.pop('explanation')
        self.assertEqual(station.learned(bare, library()), [])
        self.assertIsNone(station.hold(bare, library()))

    def test_upcoming_is_bounded(self):
        crowded = program(upcoming=[{'name': 'Track %d' % index} for index in range(30)])
        self.assertEqual(len(station.upcoming(crowded, library())), 8)


class SourceTests(unittest.TestCase):
    def test_a_radio_source_becomes_a_playable_station(self):
        value = station.station_from_source({'id': 'somafm-groove-salad', 'name': 'SomaFM Groove Salad',
                                             'url': 'https://somafm.com/groovesalad.pls'})
        self.assertEqual(value['name'], 'SomaFM Groove Salad')
        self.assertEqual(value['sources'][0]['url'], 'https://somafm.com/groovesalad.pls')
        self.assertEqual(value['sources'][0]['kind'], 'radio')
        self.assertEqual(value['end_policy'], 'stop')
        self.assertEqual(value['generation'], {'kind': 'collection'})

    def test_a_channel_keeps_the_resolver_flag(self):
        value = station.station_from_source({'id': 'lofigirl', 'name': 'lofigirl',
                                             'url': 'https://www.twitch.tv/lofigirl', 'resolver': True})
        self.assertTrue(value['sources'][0]['resolver'])

    def test_a_library_item_is_saved_as_a_reference_only(self):
        item = {'provider': 'jellyfin', 'id': 'album-1', 'type': 'MusicAlbum', 'name': 'Neoclassical collection',
                'artist': 'Various', 'genres': ['Neoclassical'], 'url': 'https://private.example.test/?token=1'}
        value = station.station_from_source(item)
        self.assertEqual(value['sources'][0], {'reference': {key: item[key] for key in
                                                            ('provider', 'id', 'type', 'name', 'artist', 'genres')}})
        self.assertNotIn('url', value['sources'][0])


class LinkTests(unittest.TestCase):
    URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

    def setUp(self):
        import next_links
        self.links = next_links

    def test_the_registered_uri_and_preview_flag_are_recognised(self):
        uri = 'listening-room://open?url=' + 'https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ'
        self.assertEqual(self.links.launch_link(['listening-room', '--ui=next', uri]), self.URL)
        self.assertEqual(self.links.launch_link(['listening-room', '--preview-link', self.URL]), self.URL)
        self.assertEqual(self.links.launch_link(['listening-room', '--preview-link=' + self.URL]), self.URL)
        self.assertEqual(self.links.launch_link(['listening-room', '--ui=next', self.URL]), self.URL)

    def test_other_command_lines_never_produce_a_link(self):
        self.assertIsNone(self.links.launch_link(['/repo/tests/probes/listening-listen-ui.py', '--output', '/tmp/x']))
        self.assertIsNone(self.links.launch_link(['/repo/tests/probes/listening-listen-ui.py',
                                                  'https://example.test/a']))
        self.assertIsNone(self.links.launch_link(['listening-room', '--ui=next']))
        self.assertIsNone(self.links.launch_link([]))
        self.assertIsNone(self.links.launch_link(['listening-room', 'listening-room://open?url=javascript:alert(1)']))
        self.assertIsNone(self.links.launch_link(['listening-room', 'https://user:secret@example.test/a']))
        self.assertIsNone(self.links.launch_link(['listening-room', 'not a url']))
        # `--ui` is listening-room's own flag, so it marks an intended launch.
        self.assertEqual(self.links.launch_link(['listening-room', '--desk', 'https://example.test/a']),
                         'https://example.test/a')

    def test_only_the_destinations_a_link_supports_are_offered(self):
        youtube = {'actions': ['music', 'atmosphere', 'picture', 'original']}
        self.assertEqual(self.links.destinations(youtube),
                         [('music', 'Play'), ('atmosphere', 'Play as ambience'),
                          ('picture', 'Show as scene'), ('original', 'Open elsewhere')])
        stream = {'actions': ['music', 'atmosphere', 'original']}
        self.assertEqual([name for _key, name in self.links.destinations(stream)],
                         ['Play', 'Play as ambience', 'Open elsewhere'])
        self.assertEqual(self.links.destinations({'actions': ['original']}), [('original', 'Open elsewhere')])
        self.assertEqual(self.links.destinations({}), [])

    def test_the_link_line_names_the_host(self):
        preview = {'url': self.URL}
        self.assertEqual(self.links.detail(preview), 'www.youtube.com · link')
        self.assertEqual(self.links.detail({}), 'Link')
        self.assertEqual(self.links.host('https://example.test/a'), 'example.test')
        self.assertEqual(self.links.host('not a url'), '')


if __name__ == '__main__':
    unittest.main()
