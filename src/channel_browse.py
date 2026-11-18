"""Pure projection helpers for the Twitch channel browser."""
import html
import re


def highlight(value, query):
    """Escape all content and emphasize literal search matches for Pango."""
    value = str(value or '')
    query = query.strip()
    if not query:
        return html.escape(value)
    pieces = re.split('(' + re.escape(query) + ')', value, flags=re.IGNORECASE)
    return ''.join('<b>' + html.escape(part) + '</b>' if i % 2 else html.escape(part)
                   for i, part in enumerate(pieces))


def project(channels, query='', saved_only=False, saved=(), excluded=()):
    """Return visible channels with membership flags without changing the source."""
    saved = {s.lower() for s in saved}
    excluded = {s.lower() for s in excluded}
    query = query.strip().casefold()
    result = []
    for channel in channels:
        login = channel['login'].lower()
        is_saved = login in saved
        if saved_only and not is_saved:
            continue
        if query and not any(query in str(channel.get(key) or '').casefold()
                             for key in ('name', 'login', 'title')):
            continue
        result.append(dict(channel, saved=is_saved, excluded=login in excluded))
    return result
