"""O10-LISTEN: Twitch categories, preview data and paging (fixtures only), the channel row words and ordering,
and World radio ranking. No GTK and no network."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import channel_browse as browse
import player_core as core
import twitch_channels as twitch


def helix(rows, cursor=None):
    body = {'data': rows}
    if cursor:
        body['pagination'] = {'cursor': cursor}
    return body


def stream(login, game='42', viewers=10, **extra):
    return dict({'user_login': login, 'user_name': login.title(), 'game_id': game, 'game_name': 'Music',
                 'title': 'live ' + login, 'viewer_count': viewers, 'language': 'en',
                 'thumbnail_url': 'https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{width}x{height}.jpg'.replace('{login}', login)}, **extra)


class TwitchPaging(unittest.TestCase):
    def setUp(self):
        stored = patch.object(twitch.local_credentials, 'load', return_value=twitch.local_credentials.Credentials('c', 't', 'x'))
        stored.start()
        self.addCleanup(stored.stop)

    def test_first_page_carries_cursor_viewers_language_and_preview(self):
        replies = [helix([{'id': '42', 'name': 'Music'}]), helix([stream('a', viewers=1200), stream('b', viewers=5)], 'next-1')]
        with patch.object(twitch, '_json_get', side_effect=replies) as get:
            result = twitch.discover('music', curated=[])
        self.assertEqual(result['cursor'], 'next-1')
        self.assertEqual(result['game_id'], '42')
        first = result['channels'][0]
        self.assertEqual((first['viewers'], first['language'], first['category']), (1200, 'EN', 'Music'))
        self.assertTrue(first['thumbnail'].endswith('live_user_a-160x90.jpg'))
        self.assertIn('first=100', get.call_args_list[1].args[0].full_url)

    def test_next_page_sends_after_and_skips_the_category_lookup_and_curated(self):
        with patch.object(twitch, '_json_get', side_effect=[helix([stream('c')], None)]) as get:
            result = twitch.discover('music', curated=['mine'], after='next-1', game_id='42')
        self.assertEqual(get.call_count, 1)
        url = get.call_args.args[0].full_url
        self.assertIn('after=next-1', url)
        self.assertIn('game_id=42', url)
        self.assertIsNone(result['cursor'])
        self.assertEqual([c['login'] for c in result['channels']], ['c'])

    def test_more_than_sixty_channels_are_all_kept(self):
        rows = [stream('ch%03d' % n, viewers=1000 - n) for n in range(100)]
        with patch.object(twitch, '_json_get', side_effect=[helix([{'id': '42'}]), helix(rows, 'more')]):
            result = twitch.discover('music', curated=[])
        self.assertEqual(len(result['channels']), 100)

    def test_asmr_is_its_own_list(self):
        with patch.object(twitch, '_json_get', side_effect=[helix([{'id': '43', 'name': 'ASMR'}]),
                                                            helix([stream('soft', '43', 3, game_name='ASMR')])]) as get:
            result = twitch.discover('asmr', curated=[])
        self.assertIn('name=ASMR', get.call_args_list[0].args[0].full_url)
        self.assertEqual(result['channels'][0]['category'], 'ASMR')

    def test_other_category_by_name_and_id(self):
        with patch.object(twitch, '_json_get', side_effect=[helix([stream('x', '77', game_name='Talk Shows & Podcasts')])]) as get:
            result = twitch.discover('music', curated=[], category='Talk Shows & Podcasts', game_id='77')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result['channels'][0]['category'], 'Talk Shows & Podcasts')

    def test_category_search(self):
        with patch.object(twitch, '_json_get', return_value=helix([{'id': '9', 'name': 'Podcasts'}, {'name': 'no id'}])) as get:
            rows = twitch.search_categories('pod')
        self.assertEqual(rows, [{'id': '9', 'name': 'Podcasts'}])
        self.assertIn('search/categories', get.call_args.args[0].full_url)
        with patch.object(twitch, '_json_get', side_effect=OSError('down')):
            with self.assertRaises(OSError):
                twitch.search_categories('pod')

    def test_preview_addresses_are_only_twitch_images(self):
        self.assertTrue(twitch.thumbnail('https://static-cdn.jtvnw.net/x-{width}x{height}.jpg'))
        for bad in ('http://static-cdn.jtvnw.net/x.jpg', 'https://example.org/x.jpg', 'file:///etc/passwd',
                    'https://jtvnw.net.example.org/x.jpg', ''):
            self.assertEqual(twitch.thumbnail(bad), '', bad)


class SavedCategories(unittest.TestCase):
    def test_saved_categories_round_trip_and_survive_channel_edits(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
            twitch.save_categories([{'id': '9', 'name': 'Podcasts'}, {'id': 'x', 'name': 'bad id'}, {'id': '9', 'name': 'again'}])
            self.assertEqual(twitch.saved_categories(), [{'id': '9', 'name': 'Podcasts'}])
            twitch.save_preferences('music', ['lofigirl'], [])
            self.assertEqual(twitch.saved_categories(), [{'id': '9', 'name': 'Podcasts'}])
            self.assertEqual(twitch.curated_channels('music'), ['lofigirl'])
            twitch.save_categories([])
            self.assertEqual(twitch.saved_categories(), [])
            self.assertEqual(twitch.curated_channels('music'), ['lofigirl'])
            data = json.loads(twitch.preferences_path().read_text())
            self.assertEqual(set(data), {'music', 'excluded', 'categories'})


class ChannelWords(unittest.TestCase):
    def test_viewers(self):
        self.assertEqual([browse.viewers_text(n) for n in (0, 845, 1000, 1234, 15600, 120000)],
                         ['0 watching', '845 watching', '1 k watching', '1.2 k watching', '15.6 k watching', '120 k watching'])
        self.assertEqual(browse.viewers_text(None), '')

    def test_detail(self):
        live = {'live': True, 'category': 'Music', 'viewers': 1234, 'language': 'en'}
        self.assertEqual(browse.channel_detail(live), 'Music · 1.2 k watching · EN')
        self.assertEqual(browse.channel_detail({'live': False}), 'Offline')
        self.assertEqual(browse.channel_detail({'live': None}), 'Live channel · Twitch')
        self.assertEqual(browse.channel_detail({'live': True}), 'Live channel · Twitch')

    def test_arrange_orders_and_never_drops(self):
        rows = [{'login': 'a', 'viewers': 5}, {'login': 'b', 'viewers': 50, 'curated': True}, {'login': 'c', 'viewers': 500},
                {'login': 'z', 'live': False}]
        self.assertEqual([r['login'] for r in browse.arrange(rows, 'most')], ['c', 'b', 'a', 'z'])
        self.assertEqual([r['login'] for r in browse.arrange(rows, 'fewest')], ['a', 'b', 'c', 'z'])
        self.assertEqual([r['login'] for r in browse.arrange(rows, 'yours')], ['b', 'c', 'a', 'z'])

    def test_interleave_takes_turns_and_dedupes(self):
        music = [{'login': 'm1'}, {'login': 'm2'}, {'login': 'm3'}]
        asmr = [{'login': 'a1'}, {'login': 'm2'}]
        self.assertEqual([c['login'] for c in browse.interleave([music, asmr])], ['m1', 'a1', 'm2', 'm3'])


class RadioRanking(unittest.TestCase):
    ROWS = [{'name': 'Kiss FM Ukrainian', 'tags': [], 'clickcount': 900},
            {'name': 'Radio Ukraine rainbow', 'tags': [], 'clickcount': 800},
            {'name': 'Akashvani FM Rainbow Kolkata', 'tags': [], 'clickcount': 700},
            {'name': 'Gentle Rain FM', 'tags': ['nature'], 'clickcount': 50},
            {'name': 'Night sounds', 'tags': ['rain', 'ambient'], 'clickcount': 10},
            {'name': 'Rain', 'tags': [], 'clickcount': 80}]

    def test_word_and_tag_matches_come_before_substring_noise(self):
        names = [r['name'] for r in core.rank_radio(self.ROWS, 'rain')]
        self.assertEqual(names[:3], ['Rain', 'Gentle Rain FM', 'Night sounds'])
        self.assertEqual(names[-1], 'Kiss FM Ukrainian')
        self.assertLess(names.index('Radio Ukraine rainbow'), names.index('Kiss FM Ukrainian'))
        self.assertEqual(len(names), len(self.ROWS))

    def test_ties_keep_the_directory_order_without_counts(self):
        rows = [{'name': 'B rain'}, {'name': 'A rain'}]
        self.assertEqual([r['name'] for r in core.rank_radio(rows, 'rain')], ['B rain', 'A rain'])

    def test_field_searches_keep_the_directory_order(self):
        rows = [{'name': 'one', 'clickcount': 1}, {'name': 'two', 'clickcount': 9}]
        self.assertEqual([r['name'] for r in core.rank_radio(rows, 'tag:jazz')], ['one', 'two'])

    def test_search_radio_ranks_what_the_directory_returned(self):
        data = [dict(name=r['name'], url_resolved='https://radio.example.test/' + str(n), stationuuid=str(n), tags=','.join(r['tags']),
                     clickcount=r['clickcount']) for n, r in enumerate(self.ROWS)]
        with patch.object(core.socket, 'getaddrinfo', side_effect=OSError('offline')), \
             patch.object(core.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(data).encode())):
            names = [r['name'] for r in core.search_radio('rain', cache=False)]
        self.assertEqual(names[0], 'Rain')
        self.assertEqual(names[-1], 'Kiss FM Ukrainian')


if __name__ == '__main__':
    unittest.main()
