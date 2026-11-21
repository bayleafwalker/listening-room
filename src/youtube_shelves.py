"""YouTube suggestions before you type, without an account.

Shelves are ordinary yt-dlp reads (see youtube_search): "For ‹room›" is built from the
active room's own tags and subtitle, "Live now" is a live-only search. Nothing about the
listener leaves the machine except those derived queries, and nothing plays until a row is
chosen. The last results of each shelf are kept on disk with their fetch time, shown at
once, refreshed at most hourly and dropped after a day. A failed or cancelled refresh keeps
what was there.
"""
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

import youtube_search

REFRESH_S = 3600.          # refresh a shelf at most hourly
MAX_AGE_S = 86400.         # never show results older than a day
RETRY_S = 600.             # after a failed refresh, wait before trying again
ROOM_LIMIT = 8             # results per room query
LIVE_LIMIT = 12
SHELF_ROWS = 12
LIVE_QUERY = 'live music radio'
# YouTube's own "Live" search filter (sp=EgJAAQ==); yt-dlp reads it through youtube:search_url.
LIVE_FILTER = 'EgJAAQ%3D%3D'


def cache_path():
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return root / 'listening-hub' / 'youtube-shelves.json'


def _clean(text, limit=80):
    return ' '.join(''.join(c for c in str(text or '') if c.isprintable()).split())[:limit]


def plan(room=None):
    """The shelves to show, as dicts with key, title and queries; [] queries means no network."""
    shelves = []
    room = room if isinstance(room, dict) else {}
    tags = [_clean(tag, 40) for tag in room.get('tags') or [] if _clean(tag, 40)]
    subtitle = _clean(room.get('subtitle'))
    name = _clean(room.get('name'), 60)
    if room.get('id') and name and (tags or subtitle):
        queries = []
        if tags:
            queries.append(' '.join(tags[:3]) + ' music')
        if subtitle:
            queries.append(subtitle + ' ambience')
        shelves.append({'key': 'room:' + _clean(room['id'], 80), 'title': 'For ' + name, 'queries': queries})
    shelves.append({'key': 'live', 'title': 'Live now', 'queries': [LIVE_QUERY], 'live': True})
    return shelves


def live_command(query, limit=LIVE_LIMIT):
    url = 'https://www.youtube.com/results?search_query=%s&sp=%s' % (urllib.parse.quote_plus(query), LIVE_FILTER)
    return [youtube_search.executable() or 'yt-dlp', '--ignore-config', '--flat-playlist', '--dump-json',
            '--no-warnings', '--skip-download', '--socket-timeout', '10', '--playlist-end', str(int(limit)), url]


def _clean_rows(rows):
    """Rows rebuilt from validated ids, exactly as a search would make them (the cache is not trusted)."""
    lines = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            lines.append(json.dumps({'id': row.get('id'), 'title': row.get('name'), 'channel': row.get('channel'),
                                     'live_status': 'is_live' if row.get('live') is True else None,
                                     'duration': _seconds(row.get('duration'))}))
    return youtube_search.parse(lines)


def _seconds(text):
    try:
        total = 0
        for part in str(text or '').split(':'):
            total = total * 60 + int(part)
        return total or None
    except ValueError:
        return None


def load(now=None):
    """Cached shelves by key: {'fetched_at', 'rows'}; entries older than a day are left out."""
    now = time.time() if now is None else now
    try:
        data = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return {}
    shelves = {}
    for key, entry in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(entry, dict) or not isinstance(key, str):
            continue
        fetched = entry.get('fetched_at')
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not 0 <= now - fetched <= MAX_AGE_S:
            continue
        rows = _clean_rows(entry.get('rows'))
        if rows:
            shelves[key] = {'fetched_at': float(fetched), 'rows': rows}
    return shelves


def save(shelves):
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {key: {'fetched_at': entry['fetched_at'], 'rows': entry['rows']}
            for key, entry in shelves.items() if entry.get('rows') and entry.get('fetched_at')}
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.youtube-shelves-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def stale(entry, now=None):
    now = time.time() if now is None else now
    return not entry or not entry.get('rows') or now - entry.get('fetched_at', 0) >= REFRESH_S


def fetch(shelf, cancel=None, runner=None):
    """One shelf's rows, its queries interleaved and each video once. Raises SearchError."""
    run = runner or youtube_search.run_process
    results = []
    for query in shelf['queries']:
        if shelf.get('live'):
            argv = live_command(query)
        else:
            argv = youtube_search.command(youtube_search.clean_query(query), ROOM_LIMIT)
        results.append(youtube_search.parse(run(argv, cancel, youtube_search.TIMEOUT_S)))
    rows, seen = [], set()
    for index in range(max((len(found) for found in results), default=0)):
        for found in results:
            if index < len(found) and found[index]['id'] not in seen:
                seen.add(found[index]['id'])
                rows.append(found[index])
    if shelf.get('live'):
        rows = [row for row in rows if row.get('live')] or rows
    return rows[:SHELF_ROWS]


def refresh(shelves, known, cancel=None, runner=None, now=None):
    """Refresh the stale shelves of ``shelves`` (a plan) over ``known`` (cached entries).

    Returns (entries, failed keys). A failed shelf keeps its cached entry; the cache file is
    written only when something new arrived."""
    now = time.time() if now is None else now
    entries, failed, changed = dict(known), [], False
    for shelf in shelves:
        if not shelf['queries'] or not stale(entries.get(shelf['key']), now):
            continue
        if cancel is not None and cancel.is_set():
            break
        try:
            rows = fetch(shelf, cancel, runner)
        except youtube_search.SearchError:
            failed.append(shelf['key'])
            continue
        if rows:
            entries[shelf['key']] = {'fetched_at': now, 'rows': rows}
            changed = True
        else:
            failed.append(shelf['key'])
    if changed:
        try:
            save(entries)
        except OSError:
            pass
    return entries, failed


def age_words(fetched_at, now=None):
    """'Updated just now', 'Updated 5 min ago', 'Updated 2 h ago'."""
    now = time.time() if now is None else now
    minutes = max(0, int((now - fetched_at) // 60))
    if minutes < 1:
        return 'Updated just now'
    if minutes < 60:
        return 'Updated %d min ago' % minutes
    return 'Updated %d h ago' % (minutes // 60)


def saved_videos(stations, history):
    """YouTube videos the listener saved as stations or played from one, newest history first; no network."""
    found, seen = [], set()

    def add(source, fallback):
        if not isinstance(source, dict):
            return
        url = source.get('url') or ''
        if source.get('source_id') != 'youtube-video' and 'youtube.com/watch' not in url:
            return
        rows = youtube_search.parse([json.dumps({'id': urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                                                 .get('v', [''])[0], 'title': source.get('name') or fallback})])
        if rows and rows[0]['id'] not in seen:
            seen.add(rows[0]['id'])
            found.append(dict(rows[0], channel='Your station'))

    for entry in history or []:
        station = entry.get('station') if isinstance(entry, dict) and isinstance(entry.get('station'), dict) else {}
        for source in station.get('sources') or []:
            add(source, station.get('name'))
    for station in stations or []:
        if isinstance(station, dict):
            for source in station.get('sources') or []:
                add(source, station.get('name'))
    return found
