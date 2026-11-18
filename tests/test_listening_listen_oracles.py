"""Independent pure oracles for the Listen surface (O3 acceptance author; not the implementer's tests).

Written from the requirements, not from the implementation: the mood mapping table, Undo as a
whole-value round trip, the Replay rule, reordering as a permutation, the link destinations per
kind of link, and that malformed links never become a preview or a launch link. The GTK behaviour is
checked by tests/probes/listening-o3-acceptance.py.
"""
import copy
import itertools
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import next_links
import next_station as ns
import online_services
import station_engine

STATION = {
    'id': 'station-x', 'name': 'X', 'sources': [{'reference': {'provider': 'jellyfin', 'id': 'a', 'type': 'MusicAlbum', 'name': 'A'}}],
    'fallbacks': [{'source_id': 'f%d' % i, 'name': 'F%d' % i, 'url': 'https://r.example.test/%d' % i, 'kind': 'radio'} for i in range(4)],
    'preferences': {'energy': 'Any', 'familiarity': 'Any', 'speech': 'Less speech', 'stay_close': 'Explore'},
    'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'}}

MAPPING = {'energy': {'Calm': ('Low',), 'Any': ('Any',), 'Lively': ('High',)},
           'familiarity': {'Familiar': ('Familiar',), 'A mix': ('Any',), 'Discover': ('New',)},
           'speech': {'Any': ('Any',), 'Fewer': ('Less speech', 'Likely instrumental'), 'Instrumental only': ('Strict instrumental',)}}


class MoodOracles(unittest.TestCase):
    def test_every_choice_maps_to_a_value_the_engine_accepts(self):
        for key, choices in MAPPING.items():
            for label, allowed in choices.items():
                value = ns.value_for(key, label)
                self.assertIn(value, allowed, (key, label))
                self.assertIn(value, station_engine.PREFERENCE_VALUES[key], (key, label))
                saved = station_engine.normalize(ns.with_choice(STATION, key, label))
                self.assertEqual(saved['preferences'][key], value)

    def test_a_choice_changes_only_its_own_key(self):
        for key, choices in MAPPING.items():
            for label in choices:
                changed = ns.with_choice(STATION, key, label)
                before, after = STATION['preferences'], changed['preferences']
                self.assertEqual({k for k in before if before[k] != after.get(k)} - {key}, set())
                self.assertEqual({k: v for k, v in changed.items() if k != 'preferences'},
                                 {k: v for k, v in STATION.items() if k != 'preferences'})
                self.assertEqual(STATION['preferences']['speech'], 'Less speech', 'the input must not be mutated')

    def test_undo_is_a_whole_value_round_trip_even_for_less_speech(self):
        before = copy.deepcopy(STATION)
        changed = ns.with_choice(STATION, 'speech', 'Instrumental only')
        self.assertEqual(before, STATION)
        self.assertEqual(ns.choices(before)['speech'], 'Fewer')
        self.assertEqual(before['preferences']['speech'], 'Less speech')
        self.assertNotEqual(changed['preferences']['speech'], 'Less speech')

    def test_unreachable_stored_values_survive_other_changes(self):
        held = dict(STATION, preferences=dict(STATION['preferences'], speech='Speech welcome'))
        changed = ns.with_choice(held, 'energy', 'Lively')
        self.assertEqual(changed['preferences']['speech'], 'Speech welcome')
        self.assertEqual(changed['preferences']['stay_close'], 'Explore')

    def test_unknown_choices_are_refused(self):
        with self.assertRaises(ValueError):
            ns.value_for('speech', 'Whatever')
        with self.assertRaises(Exception):
            ns.value_for('nonsense', 'Any')

    def test_arc_and_end_labels(self):
        for label, value in (('Steady', 'none'), ('Wind down', 'wind_down'), ('Rise', 'rise')):
            self.assertEqual(ns.with_arc(STATION, label)['arc'], value)
        for label, value in (('Stop', 'stop'), ('Repeat', 'repeat')):
            self.assertEqual(ns.with_end(STATION, label)['end_policy'], value)


class HintOracles(unittest.TestCase):
    def test_hint_only_for_a_constrained_voices_value(self):
        for speech in ('Any', None, 'Speech welcome'):
            station = dict(STATION, preferences=dict(STATION['preferences'], speech=speech))
            self.assertFalse(ns.hints(station, 5), speech)
        strict = dict(STATION, preferences=dict(STATION['preferences'], speech='Strict instrumental'))
        self.assertIn('3 reviewed recordings', ns.hints(strict, 3))
        self.assertIn('1 reviewed recording ', ns.hints(strict, 1) + ' ')
        self.assertNotIn('1 reviewed recordings', ns.hints(strict, 1))
        self.assertIn('130 reviewed recordings', ns.hints(strict, 130))

    def test_energy_and_familiarity_never_get_a_reviewed_count(self):
        for energy in ('Low', 'High'):
            station = dict(STATION, preferences=dict(STATION['preferences'], energy=energy, speech='Any'))
            self.assertFalse(ns.hints(station, 3))


class ReplayOracles(unittest.TestCase):
    def test_replay_only_when_the_entry_kept_track_references(self):
        track = {'provider': 'jellyfin', 'id': 't', 'type': 'Audio', 'name': 'T'}
        entries = [{'id': 'a', 'station': STATION, 'time': 1, 'tracks': [track]},
                   {'id': 'b', 'station': STATION, 'time': 1, 'tracks': []},
                   {'id': 'c', 'station': STATION, 'time': 1, 'tracks': [None, 'x', 7]},
                   {'id': 'd', 'station': STATION, 'time': 1},
                   {'id': 'e', 'station': STATION, 'time': 1, 'tracks': 'nope'},
                   {'id': 'f', 'time': 1, 'tracks': [track]},
                   'junk', None]
        rows = ns.history_rows(entries, 1000)
        self.assertEqual({row['id']: row['action'] for row in rows},
                         {'a': 'replay', 'b': 'tune', 'c': 'tune', 'd': 'tune', 'e': 'tune', 'f': 'replay'})


class ReorderOracles(unittest.TestCase):
    def test_moved_is_always_a_permutation_and_ignores_bad_indexes(self):
        items = list('abcd')
        for source, target in itertools.product(range(-2, 7), repeat=2):
            result = ns.moved(items, source, target)
            self.assertEqual(sorted(result), items)
            if not (0 <= source < 4 and 0 <= target < 4) or source == target:
                self.assertEqual(result, items)
            else:
                self.assertEqual(result[target], items[source])
        self.assertEqual(items, list('abcd'))

    def test_with_fallbacks_keeps_order_and_everything_else(self):
        moved = ns.moved(STATION['fallbacks'], 3, 0)
        saved = station_engine.normalize(ns.with_fallbacks(STATION, moved))
        self.assertEqual([f['name'] for f in saved['fallbacks']], ['F3', 'F0', 'F1', 'F2'])
        self.assertEqual(saved['preferences'], STATION['preferences'])


class LinkOracles(unittest.TestCase):
    def kinds(self, url):
        return [key for key, _ in next_links.destinations(online_services.preview_link(url))]

    def test_destinations_follow_what_each_link_supports(self):
        self.assertEqual(self.kinds('https://www.youtube.com/watch?v=dQw4w9WgXcQ'), ['music', 'atmosphere', 'picture', 'original'])
        self.assertEqual(self.kinds('https://www.twitch.tv/lofigirl'), ['music', 'atmosphere', 'original'])
        self.assertEqual(self.kinds('https://radio.example.test/live.mp3'), ['music', 'atmosphere', 'original'])
        self.assertEqual(self.kinds('https://example.test/page'), ['original'])
        self.assertEqual(self.kinds('https://www.youtube.com/@someone'), ['original'])

    def test_malformed_links_are_refused_everywhere(self):
        for bad in ('file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x', 'ftp://example.test/a.mp3', '',
                    'https://', 'https:///a.mp3', 'https://u:p@example.test/a.mp3', 'https://exa mple.test/', 'https://example.test:99999/',
                    'https://example.test/a\nb', 'https://example.test/' + 'a' * 5000):
            with self.assertRaises(Exception, msg=bad):
                online_services.preview_link(bad)
            self.assertIsNone(next_links._public(bad), bad)

    def test_launch_link_never_yields_a_malformed_link(self):
        for bad in ('file:///etc/passwd', 'javascript:alert(1)', 'https://u:p@example.test/a.mp3'):
            self.assertIsNone(next_links.launch_link(['listening-room', '--ui=next', bad]), bad)
            self.assertIsNone(next_links.launch_link(
                ['listening-room', '--ui=next', 'listening-room://open?url=' + bad]), bad)
        good = 'https://radio.example.test/live.mp3'
        self.assertEqual(next_links.launch_link(['listening-room', '--ui=next', good]), good)
        self.assertEqual(next_links.launch_link(
            ['listening-room', '--ui=next', 'listening-room://open?url=https%3A%2F%2Fradio.example.test%2Flive.mp3']), good)
        self.assertIsNone(next_links.launch_link(['listening-room', 'listening-room://other?url=' + good]))
        self.assertIsNone(next_links.launch_link(['python3', 'something.py', good]))


class NonPublicOracles(unittest.TestCase):
    def test_non_public_hosts_in_every_spelling(self):
        for bad in ('http://localhost/a', 'http://LOCALHOST/a', 'http://localhost./a', 'http://127.0.0.1/a', 'http://2130706433/a',
                    'http://0x7f000001/a', 'http://0177.0.0.1/a', 'http://127.1/a', 'http://[::1]/a', 'http://[::ffff:127.0.0.1]/a',
                    'http://[::ffff:c0a8:1]/a', 'http://0.0.0.0/a', 'http://[::]/a', 'http://10.1.2.3/a', 'http://172.16.0.1/a',
                    'http://192.168.0.9/a', 'http://169.254.1.1/a', 'http://[fe80::1]/a', 'http://printer.local/a', 'http://nas.home.arpa/a',
                    'http://intranet/a'):
            self.assertTrue(next_links.non_public(bad), bad)

    def test_public_hosts_are_not_refused(self):
        for good in ('https://radio.example.test/live.mp3', 'http://93.184.216.34/a.mp3', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                     'https://www.twitch.tv/lofigirl', 'http://[2606:2800:220:1::1]/a'):
            self.assertFalse(next_links.non_public(good), good)


class RecountHintOracles(unittest.TestCase):
    def test_lower_bound_and_whole_library_wording(self):
        strict = dict(STATION, preferences=dict(STATION['preferences'], speech='Strict instrumental'))
        self.assertIn('130 reviewed recordings available across your whole library', ns.hints(strict, 130, False))
        self.assertIn('10000+ reviewed recordings', ns.hints(strict, 10000, True))
        self.assertNotIn('+', ns.hints(strict, 3, False).split(' available')[0])


class UndoOfOracles(unittest.TestCase):
    def test_undo_of_restores_only_its_key_on_top_of_the_current_station(self):
        first = ns.with_choice(STATION, 'energy', 'Lively')
        undo = ns.undo_of(STATION, 'energy')
        later = ns.with_choice(ns.with_choice(first, 'speech', 'Instrumental only'), 'familiarity', 'Discover')
        restored = undo(later)
        self.assertEqual(restored['preferences']['energy'], 'Any')
        self.assertEqual(restored['preferences']['speech'], 'Strict instrumental')
        self.assertEqual(restored['preferences']['familiarity'], 'New')
        self.assertEqual(restored['preferences']['stay_close'], 'Explore')
        self.assertEqual(undo(restored), restored, 'idempotent')

    def test_reinserted_is_clamped_and_not_duplicated(self):
        entry = STATION['fallbacks'][1]
        removed = ns.with_fallbacks(STATION, [f for f in STATION['fallbacks'] if f is not entry])
        back = ns.reinserted(removed, entry, 1)
        self.assertEqual(back['fallbacks'], STATION['fallbacks'])
        self.assertEqual(ns.reinserted(back, entry, 1)['fallbacks'], STATION['fallbacks'])
        self.assertEqual(ns.reinserted(ns.with_fallbacks(STATION, []), entry, 9)['fallbacks'], [entry])


if __name__ == '__main__':
    unittest.main()
