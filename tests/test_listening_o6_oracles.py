"""Independent oracles for O6's pure logic (acceptance author's; offline, no GTK).

The behavioural oracles live in tests/probes/listening-o6-acceptance.py; these pin the contracts that do not
need a window: what an untrusted YouTube search result can become, that a search can be killed, what a station
made from scratch may contain, and that listener-facing errors carry no codes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import channel_browse
import next_model
import next_station
import youtube_search


def row(identity, **extra):
    return json.dumps(dict({'id': identity, 'title': 'T', 'channel': 'C', 'duration': 60}, **extra))


class UntrustedSearchResults(unittest.TestCase):
    def test_only_well_formed_video_ids_become_urls(self):
        evil = ['../../etc/passwd', 'a' * 12, 'abc def ghij', 'abcdefghij;', 'abcdefghij\n', "' OR 1=1 --", '', None]
        rows = youtube_search.parse([row(x) for x in evil] + [row('abcdefghijk')])
        self.assertEqual([r['id'] for r in rows], ['abcdefghijk'])
        self.assertEqual(rows[0]['url'], 'https://www.youtube.com/watch?v=abcdefghijk')

    def test_a_url_field_from_the_search_is_never_used(self):
        rows = youtube_search.parse([row('abcdefghijk', url='https://evil.example/x', webpage_url='https://evil.example/y')])
        self.assertEqual(rows[0]['url'], 'https://www.youtube.com/watch?v=abcdefghijk')

    def test_titles_are_bounded_single_line_text(self):
        rows = youtube_search.parse([row('abcdefghijk', title='x\n\ty' * 500, channel='c\r\nd' * 500)])
        self.assertLessEqual(len(rows[0]['name']), 300)
        self.assertLessEqual(len(rows[0]['channel']), 200)
        self.assertNotRegex(rows[0]['name'] + rows[0]['channel'], r'[\r\n\t]')

    def test_non_object_and_garbage_lines_are_ignored(self):
        self.assertEqual(youtube_search.parse(['', 'null', '[1]', '"x"', '{', '{"id": ["abcdefghijk"]}']), [])

    def test_the_query_is_one_argument_and_cannot_become_an_option(self):
        with mock.patch.object(youtube_search, 'executable', return_value='/fixture/yt-dlp'):
            argv = youtube_search.command('--exec rm -rf ~; $(touch x) `id`', 5)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-1], 'ytsearch5:--exec rm -rf ~; $(touch x) `id`')
        self.assertEqual([a for a in argv if a.startswith('ytsearch')], [argv[-1]])
        self.assertTrue(argv[0].endswith('yt-dlp'))

    def test_queries_are_cleaned_and_capped(self):
        self.assertEqual(youtube_search.clean_query('  rain \x00\x07 sounds\n'), 'rain  sounds')
        self.assertEqual(len(youtube_search.clean_query('x' * 1000)), 200)

    def test_missing_yt_dlp_is_a_clear_error_not_a_crash(self):
        with mock.patch.object(youtube_search, 'executable', return_value=None):
            self.assertFalse(youtube_search.status()['available'])
            self.assertIn('yt-dlp', youtube_search.status()['text'])
            with self.assertRaises(youtube_search.SearchError) as caught:
                youtube_search.search('rain')
            self.assertIn('yt-dlp', str(caught.exception))

    def test_a_blank_query_does_not_start_a_process(self):
        with mock.patch.object(youtube_search, 'run_process') as run:
            self.assertEqual(youtube_search.search('   \x00 '), [])
        run.assert_not_called()

    def test_cancelling_kills_the_child_promptly(self):
        cancel = threading.Event()
        child = [sys.executable, '-c', 'import time; time.sleep(30)']
        outcome = []

        def work():
            try:
                youtube_search.run_process(child, cancel, 30)
            except youtube_search.SearchError as error:
                outcome.append(str(error))
        thread = threading.Thread(target=work)
        thread.start()
        time.sleep(.4)
        started = time.monotonic()
        cancel.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(outcome, ['Search cancelled'])

    def test_a_child_that_fails_is_a_search_error(self):
        with self.assertRaises(youtube_search.SearchError):
            youtube_search.run_process([sys.executable, '-c', 'import sys; sys.exit(3)'], threading.Event(), 10)


class StationsFromScratch(unittest.TestCase):
    SOURCE = {'source_id': 'custom-stream', 'id': 'custom-stream', 'name': 'Your stream', 'url': 'http://192.168.1.20:8000/a.mp3', 'kind': 'radio'}

    def test_name_is_normalised_and_defaults_to_the_source(self):
        self.assertEqual(next_station.new_station('  Night   stream ', self.SOURCE)['name'], 'Night stream')
        self.assertEqual(next_station.new_station('   ', self.SOURCE)['name'], 'Your stream')

    def test_only_known_moods_survive(self):
        mood = next_station.MOODS[0]
        good_value = mood['choices'][0][1]
        station = next_station.new_station('X', self.SOURCE, {mood['key']: good_value, mood['key'] + '-x': 'High', 'unknown-mood': 'not-a-choice'})
        self.assertEqual(station['preferences'], {mood['key']: good_value})

    def test_the_seed_is_not_mutated_and_is_the_only_source(self):
        original = dict(self.SOURCE)
        station = next_station.new_station('X', self.SOURCE)
        self.assertEqual(self.SOURCE, original)
        self.assertEqual(len(station['sources']), 1)
        self.assertEqual(station['sources'][0]['url'], self.SOURCE['url'])

    def test_audiomuse_applies_only_to_library_seeds(self):
        library = {'source_id': 'jellyfin-x', 'id': 'jellyfin-x', 'name': 'Album', 'kind': 'library', 'reference': {'provider': 'jellyfin', 'id': 'x', 'name': 'Album', 'type': 'MusicAlbum'}}
        self.assertEqual(next_station.new_station('X', self.SOURCE, similar=True)['generation']['kind'], 'collection')
        station = next_station.new_station('X', library, similar=True)
        self.assertEqual(station['generation']['kind'], 'audiomuse')
        self.assertEqual(next_station.new_station('X', library)['generation']['kind'], 'collection')

    def test_only_links_that_can_play_as_music_become_sources(self):
        self.assertIsNone(next_station.source_from_link({'actions': ['scene'], 'url': 'https://example.test/p'}))
        self.assertIsNone(next_station.source_from_link({'actions': ['music'], 'url': ''}))
        self.assertIsNone(next_station.source_from_link(None))
        self.assertIsNone(next_station.source_from_link('https://example.test/a.mp3'))
        stream = next_station.source_from_link({'actions': ['music'], 'url': 'https://example.test/a.mp3', 'kind': 'stream'})
        self.assertEqual(stream['url'], 'https://example.test/a.mp3')
        self.assertFalse(stream.get('resolver'))
        video = next_station.source_from_link({'actions': ['music'], 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'kind': 'youtube'})
        self.assertTrue(video['resolver'])


class TwitchStates(unittest.TestCase):
    def test_every_state_has_a_listener_sentence(self):
        for connected, curated, problem, state in [(False, 0, '', 'setup'), (False, 2, '', 'curated'), (True, 0, '', 'connected'),
                                                  (True, 0, 'network', 'attention'), (False, 0, 'x', 'setup')]:
            self.assertEqual(channel_browse.availability(connected, curated, problem), state)
            self.assertTrue(channel_browse.twitch_note(state, problem).strip())
        self.assertIn('not connected', channel_browse.twitch_note('curated').lower())
        self.assertIn('Preferences', channel_browse.twitch_note('attention'))

    def test_a_connected_empty_list_says_nobody_is_live_not_that_it_is_unavailable(self):
        self.assertIn('live', channel_browse.footnote('connected', False).lower())
        self.assertIsNone(channel_browse.footnote('connected', True))


class ListenerErrors(unittest.TestCase):
    def test_codes_and_exception_names_never_reach_the_listener(self):
        for raw in ('[Errno 111] Connection refused', 'ConnectionRefusedError: [Errno 111]', '[Errno 2] No such file or directory: /run/x',
                    'Broken pipe', 'timed out', 'The socket is unreachable', '', None, 'x' * 1000):
            text = next_model.plain_error(raw)
            self.assertTrue(text.strip(), raw)
            self.assertNotIn('Errno', text)
            self.assertNotIn('Traceback', text)
            self.assertLessEqual(len(text), 200)


if __name__ == '__main__':
    unittest.main()
