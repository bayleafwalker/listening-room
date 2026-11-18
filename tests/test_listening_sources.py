"""YouTube search, new-station seeds and Twitch availability (all fixtures; nothing is fetched)."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import channel_browse
import next_links
import next_station
import youtube_search


def line(identity, title='A title', channel='A channel', duration=185, **extra):
    return json.dumps(dict({'id': identity, 'title': title, 'channel': channel, 'duration': duration}, **extra))


class YouTubeSearch(unittest.TestCase):
    def test_rows_carry_title_channel_duration_and_a_rebuilt_url(self):
        rows = youtube_search.parse([line('abcdefghijk', 'Rain', 'Quiet', 3725)])
        self.assertEqual(rows, [{'kind': 'youtube', 'id': 'abcdefghijk', 'name': 'Rain', 'channel': 'Quiet',
                                 'duration': '1:02:05', 'live': False,
                                 'url': 'https://www.youtube.com/watch?v=abcdefghijk'}])

    def test_untrusted_entries_are_dropped_or_normalised(self):
        rows = youtube_search.parse([
            'not json', '[]', line('short'), line('has space!!'), line('abcdefghijk'), line('abcdefghijk'),
            line('bbbbbbbbbbb', 'x\n y', None, None, live_status='is_live', url='https://evil.example/')])
        self.assertEqual([r['id'] for r in rows], ['abcdefghijk', 'bbbbbbbbbbb'])
        self.assertEqual(rows[1]['name'], 'x y')
        self.assertTrue(rows[1]['live'] and rows[1]['duration'] == '')
        self.assertTrue(all(r['url'].startswith('https://www.youtube.com/watch?v=') for r in rows))

    def test_durations(self):
        self.assertEqual([youtube_search.duration_text(v) for v in (59, 60, 3599, 3600, None, 0, 'x')],
                         ['0:59', '1:00', '59:59', '1:00:00', '', '', ''])

    def test_query_is_one_search_argument_and_never_an_option(self):
        with mock.patch.object(youtube_search, 'executable', return_value='/bin/yt-dlp'):
            argv = youtube_search.command('--exec rm -rf x')
        self.assertEqual(argv[-1], 'ytsearch8:--exec rm -rf x')
        self.assertIn('--ignore-config', argv)
        self.assertEqual(youtube_search.clean_query('  a\x00b\n' + 'z' * 300), 'ab' + 'z' * 198)

    def test_search_uses_the_runner_and_skips_empty_queries(self):
        seen = []

        def runner(argv, cancel, timeout):
            seen.append(argv)
            return [line('abcdefghijk')]
        self.assertEqual(youtube_search.search('  ', runner=runner), [])
        self.assertEqual(seen, [])
        self.assertEqual(len(youtube_search.search('lofi', runner=runner)), 1)
        self.assertTrue(seen[0][-1].startswith('ytsearch') and seen[0][-1].endswith(':lofi'))

    def test_missing_yt_dlp_is_stated_and_never_launched(self):
        with mock.patch.object(youtube_search, 'executable', return_value=None):
            self.assertFalse(youtube_search.available())
            self.assertEqual(youtube_search.status(), {'available': False, 'text': youtube_search.MISSING})
            with mock.patch('subprocess.Popen') as popen:
                with self.assertRaises(youtube_search.SearchError):
                    youtube_search.search('rain')
            popen.assert_not_called()
        with mock.patch.object(youtube_search, 'executable', return_value='/bin/yt-dlp'):
            self.assertTrue(youtube_search.status()['available'])

    def test_a_cancelled_search_kills_the_process(self):
        cancel = threading.Event()
        started = time.monotonic()
        threading.Timer(.2, cancel.set).start()
        with self.assertRaises(youtube_search.SearchError):
            youtube_search.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], cancel, 20)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_failed_process_is_an_error_not_an_empty_result(self):
        with self.assertRaises(youtube_search.SearchError):
            youtube_search.run_process([sys.executable, '-c', 'import sys; sys.exit(3)'], None, 10)
        out = youtube_search.run_process([sys.executable, '-c', 'print(1); print(2)'], None, 10)
        self.assertEqual([x.strip() for x in out], ['1', '2'])


class NewStationSeeds(unittest.TestCase):
    def test_pasted_links_follow_the_link_rules(self):
        stream = next_station.source_from_link(next_links.descriptor('https://radio.example.test/live.mp3'))
        self.assertEqual((stream['source_id'], stream['url'], stream.get('resolver')),
                         ('custom-stream', 'https://radio.example.test/live.mp3', None))
        video = next_station.source_from_link(next_links.descriptor('https://www.youtube.com/watch?v=abcdefghijk'))
        self.assertEqual((video['source_id'], video['resolver']), ('youtube-video', True))
        twitch = next_station.source_from_link(next_links.descriptor('https://www.twitch.tv/lofigirl'))
        self.assertEqual((twitch['source_id'], twitch['resolver']), ('twitch-channel', True))
        # A web page does not offer Play, so it cannot seed a station.
        self.assertIsNone(next_station.source_from_link(next_links.descriptor('https://example.test/page')))
        self.assertIsNone(next_station.source_from_link(None))
        # An own-network stream is allowed exactly where the link sheet allows it (it offers Play).
        local = next_station.source_from_link(next_links.descriptor('http://192.168.1.20:8000/stream.mp3'))
        self.assertEqual(local['url'], 'http://192.168.1.20:8000/stream.mp3')
        self.assertTrue(next_links.non_public(local['url']))
        for bad in ('ftp://x.example/a.mp3', 'https://user:pw@x.example/a.mp3', 'not a link'):
            with self.assertRaises(Exception):
                next_links.descriptor(bad)

    def test_a_new_station_has_a_name_a_seed_and_only_the_chosen_moods(self):
        source = next_station.source_from_video({'name': 'Lo-fi', 'url': 'https://www.youtube.com/watch?v=abcdefghijk'})
        station = next_station.new_station('  Late   focus ', source, {'energy': 'Low', 'speech': 'Bogus', 'x': 'Y'})
        self.assertEqual(station['name'], 'Late focus')
        self.assertEqual(station['preferences'], {'energy': 'Low'})
        self.assertEqual(station['sources'][0]['url'], 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertTrue(station['sources'][0]['resolver'])
        self.assertEqual(station['generation'], {'kind': 'collection'})
        import station_engine
        self.assertEqual(station_engine.normalize(station)['name'], 'Late focus')

    def test_library_seed_can_ask_for_an_audiomuse_mix_and_a_radio_seed_cannot(self):
        track = {'provider': 'jellyfin', 'id': 't1', 'type': 'Audio', 'name': 'Nocturne'}
        station = next_station.new_station('Mix', track, {}, similar=True)
        self.assertEqual(station['generation'], {'kind': 'audiomuse'})
        self.assertEqual(station['sources'][0]['reference']['id'], 't1')
        radio = {'id': 'r', 'name': 'R', 'url': 'https://radio.example.test/r'}
        self.assertEqual(next_station.new_station('R', radio, {}, similar=True)['generation'], {'kind': 'collection'})
        import station_engine
        station_engine.normalize(station)

    def test_every_seed_kind_passes_the_controller_validation(self):
        import station_engine
        seeds = [next_station.source_from_channel({'login': 'lofigirl', 'name': 'lofigirl'}),
                 {'id': 'world-1', 'name': 'World', 'url': 'https://radio.example.test/w'},
                 {'provider': 'jellyfin', 'id': 'a1', 'type': 'MusicAlbum', 'name': 'Album'},
                 {'provider': 'jellyfin', 'id': 'p1', 'type': 'Playlist', 'name': 'List'}]
        for seed in seeds:
            self.assertTrue(station_engine.normalize(next_station.new_station('Named', seed))['sources'])


class TwitchAvailability(unittest.TestCase):
    def test_states(self):
        self.assertEqual(channel_browse.availability(False, 0), 'setup')
        self.assertEqual(channel_browse.availability(False, 2), 'curated')
        self.assertEqual(channel_browse.availability(True, 0), 'connected')
        self.assertEqual(channel_browse.availability(True, 3, 'Twitch login expired'), 'attention')

    def test_words_say_what_is_and_is_not_known(self):
        self.assertIn('Sign in once', channel_browse.twitch_note('setup'))
        self.assertIn('not connected', channel_browse.footnote('curated', True))
        self.assertIn('Nobody is live', channel_browse.footnote('connected', False))
        self.assertIsNone(channel_browse.footnote('connected', True))
        self.assertEqual(channel_browse.twitch_note('attention', 'Login expired.'), 'Login expired.')


if __name__ == '__main__':
    unittest.main()
