"""YouTube search through yt-dlp's ``ytsearch``; no API key and no account.

Searching is a read: it never starts sound. It runs yt-dlp as a child process so a
newer query can kill the older one, and every row is rebuilt from a validated video
id, so nothing the search returns is trusted as a URL.
"""
import json
import re
import shutil
import subprocess
import threading
import time

LIMIT = 8
TIMEOUT_S = 25.
VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
MISSING = 'Install yt-dlp to search YouTube.'


class SearchError(Exception):
    """A search that could not finish; the message is safe to show."""


def executable():
    return shutil.which('yt-dlp')


def available():
    return executable() is not None


def status():
    """One truthful line for Listen and Preferences about what YouTube search can do."""
    return {'available': True, 'text': 'YouTube search works through yt-dlp. No account is needed.'} \
        if available() else {'available': False, 'text': MISSING}


def clean_query(query):
    text = ''.join(character for character in str(query or '') if character.isprintable()).strip()
    return text[:200]


def duration_text(seconds):
    """3:07 or 1:02:03; empty when the length is unknown (a live stream)."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ''
    if total <= 0:
        return ''
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return '%d:%02d:%02d' % (hours, minutes, secs) if hours else '%d:%02d' % (minutes, secs)


def parse(lines):
    """Rows from yt-dlp's one-JSON-per-line output. Entries that are not single videos are skipped."""
    rows, seen = [], set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        identity = str(entry.get('id') or '')
        if not VIDEO_ID.match(identity) or identity in seen:
            continue
        seen.add(identity)
        title = ' '.join(str(entry.get('title') or '').split())[:300] or 'YouTube video'
        channel = ' '.join(str(entry.get('channel') or entry.get('uploader') or '').split())[:200]
        seconds = entry.get('duration')
        rows.append({'kind': 'youtube', 'id': identity, 'name': title, 'channel': channel,
                     'duration': duration_text(seconds), 'live': entry.get('live_status') == 'is_live',
                     'url': 'https://www.youtube.com/watch?v=' + identity})
    return rows


def command(query, limit=LIMIT):
    return [executable() or 'yt-dlp', '--ignore-config', '--flat-playlist', '--dump-json', '--no-warnings',
            '--skip-download', '--socket-timeout', '10', 'ytsearch%d:%s' % (limit, query)]


def run_process(argv, cancel, timeout):
    """Run yt-dlp, polling the cancel event; returns stdout lines or raises SearchError."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True)
    except OSError as error:
        raise SearchError('yt-dlp could not start: ' + (error.strerror or 'error')) from error
    deadline = time.monotonic() + timeout
    output = []
    reader = threading.Thread(target=lambda: output.extend(process.stdout), daemon=True)
    reader.start()
    while process.poll() is None:
        if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
            process.kill()
            process.wait()
            reader.join(2)
            process.stdout.close()
            raise SearchError('Search cancelled' if cancel is not None and cancel.is_set() else 'YouTube took too long')
        time.sleep(.05)
    reader.join(2)
    process.stdout.close()
    if process.returncode:
        raise SearchError('YouTube search did not finish')
    return output


def search(query, cancel=None, limit=LIMIT, runner=None):
    """Video rows for a query. ``runner(argv, cancel, timeout)`` is the test seam."""
    query = clean_query(query)
    if not query:
        return []
    if not available() and runner is None:
        raise SearchError(MISSING)
    return parse((runner or run_process)(command(query, limit), cancel, TIMEOUT_S))
