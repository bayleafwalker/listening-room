"""World radio search finds the right station when the provider's first 50 rows are noise (fixtures only)."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_tree  # noqa: E402

source_tree.use_source(Path(__file__).resolve().parents[1] / 'src/listening_hub.py')
import player_core as core  # noqa: E402


def station(index, name, tags='', clicks=0):
    return {'stationuuid': 'u%d' % index, 'name': name, 'url_resolved': 'https://radio.example.test/%d' % index,
            'tags': tags, 'clickcount': clicks, 'votes': 0, 'country': '', 'countrycode': '', 'language': '', 'homepage': ''}


class FakeProvider:
    """Answers like the directory: substring name match or exact tag, by clicks, cut at ``limit``."""

    def __init__(self, rows, fail_tag=False):
        self.rows, self.requests, self.fail_tag = rows, [], fail_tag

    def __call__(self, request, timeout=None):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query).items()}
        self.requests.append(params)
        if self.fail_tag and 'tag' in params:
            raise OSError('tag lookup down')
        rows = self.rows
        if 'name' in params:
            rows = [r for r in rows if params['name'].casefold() in r['name'].casefold()]
        if 'tag' in params:
            rows = [r for r in rows if params['tag'].casefold() in [t.strip().casefold() for t in r['tags'].split(',')]]
        rows = sorted(rows, key=lambda r: -r['clickcount'])[:int(params['limit'])]
        return io.BytesIO(json.dumps(rows).encode())


class RadioPastFifty(unittest.TestCase):
    def setUp(self):
        self.noise = [station(n, 'Ukrainian hits %d' % n, clicks=10000 - n) for n in range(80)]

    def search(self, rows, query='rain', **kwargs):
        provider = FakeProvider(rows, **kwargs)
        with mock.patch.object(core.socket, 'getaddrinfo', side_effect=OSError('offline')), \
             mock.patch.object(core.urllib.request, 'urlopen', side_effect=provider):
            return [r['name'] for r in core.search_radio(query, cache=False)], provider

    def test_best_match_past_the_first_fifty_ranks_first(self):
        best = station(900, 'Rain Radio', clicks=5)
        names, provider = self.search(self.noise + [best])
        self.assertEqual(names[0], 'Rain Radio')
        self.assertEqual(len(names), core.RADIO_SHOWN)
        # The provider's own first page really was noise: the match is beyond row 50 by its order.
        ordered = sorted(self.noise + [best], key=lambda r: -r['clickcount'])
        self.assertGreater(ordered.index(best), 50)

    def test_tag_only_station_is_found_and_ranked_with_whole_words(self):
        tagged = station(901, 'Calm Waters', tags='rain,calm', clicks=3)
        names, _ = self.search(self.noise + [tagged])
        self.assertEqual(names[0], 'Calm Waters')

    def test_request_count_and_size_are_bounded(self):
        _, provider = self.search(self.noise + [station(900, 'Rain Radio')])
        self.assertLessEqual(len(provider.requests), 2)
        self.assertTrue(all(int(p['limit']) <= 250 for p in provider.requests))

    def test_two_word_and_field_queries_make_one_request(self):
        for query in ('rain radio', 'country:FI', 'tag:rain'):
            with self.subTest(query=query):
                _, provider = self.search(self.noise, query)
                self.assertEqual(len(provider.requests), 1)

    def test_a_failing_tag_request_keeps_the_name_results(self):
        names, provider = self.search(self.noise + [station(900, 'Rain Radio')], fail_tag=True)
        self.assertEqual(names[0], 'Rain Radio')
        self.assertEqual(len(provider.requests), 2)

    def test_duplicates_across_both_requests_appear_once(self):
        both = station(902, 'Rain Radio', tags='rain', clicks=9)
        names, _ = self.search(self.noise + [both])
        self.assertEqual(names.count('Rain Radio'), 1)


if __name__ == '__main__':
    unittest.main()
