"""Independent acceptance oracles for O11-HYGIENE criteria 2, 3 and 4.

Owned by the O11-HYGIENE acceptance author, not the implementer. Criterion 1 (`--source` is honoured) needs a
display and lives in tests/probes/listening-o11h-acceptance.py.

Criterion 2 is behavioural: ``listening_hub.search_radio`` (what Listen and the CLI call) runs against a fixture
Radio Browser that answers the real API (``/json/stations/search`` with name, nameExact, tag, tagExact, tagList,
order, reverse, offset and limit, and the ``byname``/``bynameexact``/``bytag``/``bytagexact`` paths, GET or POST).
The transport is faked at ``urllib.request.OpenerDirector.open`` (used by ``urlopen`` and every ``build_opener``),
DNS for radio-browser hosts fails, so nothing reaches the network. The fixture puts the best matches beyond the
provider's first 50 results under every ordering it supports, behind substring noise with more clicks, and the
query words are random per run (the canonical N12 "rain" case is kept too), so special-casing a fixture query does
not pass. A request without ``limit`` returns the whole directory, as Radio Browser does.

Criteria 3 and 4 are about text, so they are source-text checks by necessity.
"""
import email.message
import io
import json
import os
from pathlib import Path
import random
import re
import socket
import string
import sys
import time
import unittest
from unittest import mock
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))
import listening_hub as hub  # noqa: E402

# ---- criterion 2 limits -------------------------------------------------------------------------------------
# The brief says "keep request count and latency bounded" and gives no numbers. Every limit below is SELF-CHOSEN by
# the acceptance author and flagged for a coordinator ruling.
OLD_CAP = 50                 # the provider page main fetches (brief: "the provider's first 50 results")
DEPTH = (51, 150)            # SELF-CHOSEN: the best match sits at provider positions 51-150 (clickcount order)
MAX_REQUESTS = 4             # SELF-CHOSEN: provider requests per search
MAX_ROWS_REQUESTED = 1000    # SELF-CHOSEN: sum of `limit` over one search's requests
LATENCY = .25                # SELF-CHOSEN: fixture latency per provider request, seconds
MAX_SECONDS = 2.0            # SELF-CHOSEN: one search's wall time with that latency (about 4 sequential requests)
NOISE = 200                  # substring and word-start noise stations per query


def truthy(value):
    return str(value).strip().lower() in ('true', '1', 'yes')


class Response(io.BytesIO):
    def __init__(self, url, body):
        super().__init__(body)
        self.url, self.status, self.code, self.reason = url, 200, 200, 'OK'
        self.headers = email.message.Message()
        self.headers['Content-Type'] = 'application/json'
        self.headers['Content-Length'] = str(len(body))

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())


class Directory:
    """A fixture Radio Browser: the stations, the requests it served, and the API's filtering and ordering."""

    def __init__(self, stations):
        self.stations = stations
        self.requests = []

    def serve(self, url, data=None):
        parsed = urllib.parse.urlsplit(url)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        if data:
            body = data.decode() if isinstance(data, bytes) else str(data)
            try:
                params.update({k: str(v) for k, v in json.loads(body).items()})
            except ValueError:
                params.update({k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})
        path = urllib.parse.unquote(parsed.path).rstrip('/')
        match = re.fullmatch(r'/json/stations/(byname|bynameexact|bytag|bytagexact)/(.+)', path)
        if match:
            kind, term = match.groups()
            params[{'byname': 'name', 'bynameexact': 'name', 'bytag': 'tag', 'bytagexact': 'tag'}[kind]] = term
            if kind.endswith('exact'):
                params[('name' if 'name' in kind else 'tag') + 'Exact'] = 'true'
        elif path != '/json/stations/search':
            raise urllib.error.HTTPError(url, 404, 'not in the fixture: ' + path, {}, None)
        self.requests.append({'url': url, 'params': dict(params)})
        rows = list(self.stations)
        if params.get('name'):
            term = params['name'].casefold()
            exact = truthy(params.get('nameExact', ''))
            rows = [r for r in rows if (r['name'].casefold() == term if exact else term in r['name'].casefold())]
        if params.get('tag'):
            term = params['tag'].casefold()
            exact = truthy(params.get('tagExact', ''))
            rows = [r for r in rows if any((t == term if exact else term in t) for t in r['tags'].split(','))]
        if params.get('tagList'):
            wanted = [t.strip().casefold() for t in params['tagList'].split(',') if t.strip()]
            rows = [r for r in rows if all(t in r['tags'].split(',') for t in wanted)]
        order = params.get('order', 'name')
        if order == 'random':
            random.Random(len(self.requests)).shuffle(rows)
        else:
            key = order if order in ('clickcount', 'votes', 'name') else 'name'
            rows.sort(key=lambda r: r[key].casefold() if key == 'name' else r[key])
            if truthy(params.get('reverse', 'false')):
                rows.reverse()
        offset = int(params.get('offset') or 0)
        limit = int(params['limit']) if str(params.get('limit', '')).strip() else 100000   # the real API's default
        return rows[offset:offset + limit]


def pseudo_word(rng):
    consonants, vowels = 'bdfgklmnprstvz', 'aeiou'
    return ''.join(rng.choice(consonants) + rng.choice(vowels) for _ in range(2)) + rng.choice(consonants)


def fixture(word, rng, canonical_noise=()):
    """NOISE stations matching `word` only inside or at the start of a longer word, plus two whole-word matches
    (the best and second-best) placed at provider positions inside DEPTH under clickcount and votes order and past
    the first 50 under name order. Returns (stations, best_name, second_name)."""
    cap = word.capitalize()
    names = set(canonical_noise)
    while len(names) < NOISE:
        if rng.random() < .2:   # word-start noise (tier 1): "Rainbow"
            name = '%s%s %s' % (cap, rng.choice(['bow', 'er', 'ing', 'ia', 'ex']),
                                rng.choice(['FM', 'Hits', 'Radio', 'One', 'Live']) + ' ' + str(rng.randint(1, 999)))
        else:                   # substring noise (tier 2): "Ukrainian"
            prefix = rng.choice(string.ascii_uppercase) + ''.join(rng.choice('aeioulnrst') for _ in range(rng.randint(1, 4)))
            name = '%s%s%s %s' % (prefix, word, rng.choice(['ian', 'er', 'o', 'ia', 'ey', 's']),
                                  rng.choice(['FM', 'Hits', 'Radio', 'Gold']) + ' ' + str(rng.randint(1, 999)))
        names.add(name)
    noise = sorted(names)
    clicks = sorted(rng.sample(range(1000, 900000), NOISE), reverse=True)
    rng.shuffle(noise)
    stations = []
    for index, (name, count) in enumerate(zip(noise, clicks)):
        stations.append({'name': name, 'clickcount': count})
    if canonical_noise:   # the N12 offenders get the most clicks
        for station, name in zip(stations, canonical_noise):
            other = next(s for s in stations if s['name'] == name)
            station['name'], other['name'] = other['name'], station['name']
    best_rank = rng.randint(DEPTH[0] + 4, DEPTH[0] + 44)
    second_rank = rng.randint(best_rank + 10, DEPTH[1] - 5)
    best = 'Mellow %s Radio' % cap
    second = 'Morning %s' % cap
    stations.append({'name': best, 'clickcount': (clicks[best_rank - 2] + clicks[best_rank - 1]) // 2})
    stations.append({'name': second, 'clickcount': (clicks[second_rank - 2] + clicks[second_rank - 1]) // 2})
    tags = ['pop', 'news', 'talk', 'hits', 'rock', 'dance', 'oldies']
    for number, station in enumerate(stations):
        station.update(stationuuid='%s-%04d' % (word, number), url_resolved='https://radio.example.test/%s/%d' % (word, number),
                       url='https://radio.example.test/%s/%d' % (word, number), homepage='', country='Nowhere',
                       countrycode='', language='', lastcheckok=1, votes=station['clickcount'] // 7,
                       tags=','.join(rng.sample(tags, 2)))
    return stations, best, second


def positions(stations, name):
    """Where `name` sits (1-based) in the provider's own order for each ordering it supports."""
    out = {}
    for label, key, reverse in (('clickcount desc', 'clickcount', True), ('votes desc', 'votes', True),
                                ('name asc', 'name', False), ('name desc', 'name', True)):
        ordered = sorted(stations, key=lambda r: r[key].casefold() if key == 'name' else r[key], reverse=reverse)
        out[label] = [r['name'] for r in ordered].index(name) + 1
    return out


class FakeNetwork:
    """Route radio-browser URLs to the directory; anything else is refused. DNS for radio-browser hosts fails."""

    def __init__(self, directory):
        self.directory = directory
        self.real_getaddrinfo = socket.getaddrinfo

    def open(self, opener, fullurl, data=None, timeout=None, *args, **kwargs):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        if data is None and isinstance(fullurl, urllib.request.Request):
            data = fullurl.data
        host = urllib.parse.urlsplit(url).hostname or ''
        if not host.endswith('radio-browser.info'):
            raise OSError('O11H fixture: no network for ' + host)
        time.sleep(LATENCY)
        return Response(url, json.dumps(self.directory.serve(url, data)).encode())

    def getaddrinfo(self, host, *args, **kwargs):
        if 'radio-browser' in str(host):
            raise socket.gaierror('O11H fixture: no DNS')
        return self.real_getaddrinfo(host, *args, **kwargs)

    def __enter__(self):
        network = self

        def opener_open(opener, fullurl, data=None, *args, **kwargs):
            return network.open(opener, fullurl, data, *args, **kwargs)
        self.patches = [mock.patch.object(urllib.request.OpenerDirector, 'open', opener_open),
                        mock.patch.object(socket, 'getaddrinfo', self.getaddrinfo),
                        mock.patch.object(socket, 'gethostbyaddr', side_effect=socket.herror('O11H fixture: no DNS'))]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in reversed(self.patches):
            patch.stop()


SEED = int(os.environ.get('O11H_SEED', time.time_ns() % 1000000))
N12_NOISE = ('Kiss FM Ukrainian', 'Akashvani FM Rainbow Kolkata', 'Radio Ukraine rainbow', 'Brainwave FM', 'Training Hits')


class WorldRadioPast50(unittest.TestCase):
    """Criterion 2: a best match beyond the provider's first 50 results still ranks first, with bounded fetches."""

    @classmethod
    def setUpClass(cls):
        rng = random.Random(SEED)
        words = ['rain']
        while len(words) < 3:
            word = pseudo_word(rng)
            if word not in words:
                words.append(word)
        cls.cases = {}
        for index, word in enumerate(words):
            stations, best, second = fixture(word, rng, N12_NOISE if word == 'rain' else ())
            directory = Directory(stations)
            started = time.monotonic()
            error = None
            try:
                with FakeNetwork(directory):
                    found = hub.search_radio(word, cache=False)
            except Exception as exc:   # reported by every check below
                found, error = [], '%s: %s' % (type(exc).__name__, exc)
            label = 'the N12 query "rain"' if index == 0 else 'random query word %d' % index
            cls.cases[label] = {'word': word, 'stations': stations, 'best': best, 'second': second, 'found': found,
                                'error': error, 'seconds': time.monotonic() - started, 'requests': directory.requests}
            print('O11H radio %s (%r, seed %d): %d requests, %.2f s, limits %s, error %s' % (
                label, word, SEED, len(directory.requests), cls.cases[label]['seconds'],
                [r['params'].get('limit') for r in directory.requests], error), file=sys.stderr)

    def test_r0_precondition_best_matches_are_beyond_the_first_50_in_every_provider_order(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                for name in (case['best'], case['second']):
                    where = positions(case['stations'], name)
                    self.assertTrue(DEPTH[0] <= where['clickcount desc'] <= DEPTH[1], where)
                    self.assertTrue(DEPTH[0] <= where['votes desc'] <= DEPTH[1], where)
                    self.assertGreater(where['name asc'], OLD_CAP, where)
                    self.assertGreater(where['name desc'], OLD_CAP, where)

    def test_r1_the_whole_word_match_beyond_50_ranks_first(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                names = [r.get('name') for r in case['found']]
                self.assertIsNone(case['error'], case['error'])
                self.assertTrue(names and names[0] == case['best'],
                                'first results %r; best match %r sits at provider positions %r' % (
                                    names[:5], case['best'], positions(case['stations'], case['best'])))

    def test_r1b_both_whole_word_matches_come_before_all_noise_most_clicked_first(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                names = [r.get('name') for r in case['found']]
                self.assertEqual(names[:2], [case['best'], case['second']], names[:5])

    def test_r1c_no_station_is_listed_twice(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                ids = [r.get('id') for r in case['found']]
                self.assertTrue(ids, case['error'])
                self.assertEqual(len(ids), len(set(ids)), [i for i in ids if ids.count(i) > 1][:5])

    def test_r2_every_provider_request_carries_an_explicit_limit(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                self.assertTrue(case['requests'], 'no provider request at all: %s' % case['error'])
                unbounded = [r['url'] for r in case['requests'] if not str(r['params'].get('limit', '')).strip()]
                self.assertFalse(unbounded, 'requests without a limit fetch the whole directory: %r' % unbounded)

    def test_r3_request_count_is_bounded(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                self.assertLessEqual(len(case['requests']), MAX_REQUESTS,
                                     '%d requests: %r' % (len(case['requests']), [r['url'] for r in case['requests']]))

    def test_r4_rows_requested_are_bounded(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                total = sum(int(r['params'].get('limit') or 100000) for r in case['requests'])
                self.assertLessEqual(total, MAX_ROWS_REQUESTED, [r['params'].get('limit') for r in case['requests']])

    def test_r5_search_latency_is_bounded(self):
        for label, case in self.cases.items():
            with self.subTest(case=label, word=case['word'], seed=SEED):
                self.assertLessEqual(case['seconds'], MAX_SECONDS, '%.2f s for %d requests at %.2f s each' % (
                    case['seconds'], len(case['requests']), LATENCY))

    def test_r6_a_best_match_inside_the_first_50_still_ranks_first(self):
        """Regression guard: the old path keeps working when the provider's first page already holds the match."""
        rng = random.Random(SEED + 1)
        stations, best, second = fixture('fog', rng)
        for station in stations:
            if station['name'] == best:
                station['clickcount'], station['votes'] = 10 ** 7, 10 ** 6
        with FakeNetwork(Directory(stations)):
            names = [r['name'] for r in hub.search_radio('fog', cache=False)]
        self.assertEqual(names[0], best, names[:5])


# ---- criterion 3: stale text -----------------------------------------------------------------------------------

def sentences(text):
    flat = re.sub(r'\s+', ' ', re.sub(r'^\s*(#|//)\s?', ' ', text, flags=re.M))
    return re.split(r'(?<=[.!?:;])\s', flat)


DENIAL = re.compile(r'\b(has no|has none|lacks|does not (have|provide|include)|doesn\'t (have|provide|include)|'
                    r'is missing|without GStreamer)\b', re.I)


class StaleText(unittest.TestCase):
    """Criterion 3. Text checks by necessity: the criterion is that these statements are gone."""

    def test_t1_hotfix_probe_no_longer_says_the_dev_shell_has_no_gstreamer(self):
        text = (ROOT / 'tests/probes/listening-hotfix-media-acceptance.py').read_text()
        stale = [s for s in sentences(text) if re.search(r'dev ?shell', s, re.I) and DENIAL.search(s)]
        self.assertFalse(stale, stale)

    def test_t2_no_test_or_doc_says_the_dev_shell_lacks_gstreamer_in_the_present_tense(self):
        files = list((ROOT / 'tests').rglob('*.py')) + list((ROOT / 'docs').rglob('*.md')) + \
            [ROOT / 'README.md', ROOT / 'CONTRIBUTING.md']
        stale = []
        for path in files:
            if path.name == Path(__file__).name or not path.exists():
                continue
            for s in sentences(path.read_text(errors='replace')):
                if re.search(r'dev ?shell', s, re.I) and re.search(r'gstreamer|\bgst\b|\bnone\b', s, re.I) \
                        and DENIAL.search(s) and not re.search(r'\b(had|before O10|until O10)\b', s, re.I):
                    stale.append('%s: %s' % (path.relative_to(ROOT), s[:200]))
        self.assertFalse(stale, stale)


# ---- criterion 4: CHANGELOG ------------------------------------------------------------------------------------

class Changelog(unittest.TestCase):
    """Criterion 4: the user-visible change (radio search relevance) is in the CHANGELOG above 2.5.0, and the
    internal ones (probe plumbing, docstrings) are not."""

    def newest(self):
        text = (ROOT / 'CHANGELOG.md').read_text()
        head = text.split('\n## [2.5.0]')[0]
        parts = head.split('\n## ')[1:]
        return '\n'.join(parts)

    def test_c1_the_radio_search_change_is_recorded_above_2_5_0(self):
        section = self.newest()
        self.assertTrue(re.search(r'radio|station', section, re.I) and re.search(r'search|result|find|match', section, re.I),
                        'nothing about world radio search above [2.5.0]: %r' % section[:400])

    def test_c2_internal_test_plumbing_is_not_in_the_changelog(self):
        section = self.newest()
        self.assertFalse(re.search(r'o6_env|--source|LISTENING_HUB_SOURCE|docstring|\bprobes?\b', section, re.I),
                         section[:600])


if __name__ == '__main__':
    unittest.main(verbosity=2)
