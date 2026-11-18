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


# ---- what Twitch can show, and what it says about itself --------------------------

SETUP_NOTE = 'Sign in once. The channels you follow and live music streams then appear here.'
CURATED_NOTE = ('Twitch is not connected, so these channels may or may not be live. '
                'Connect it in Preferences to see who is live.')


def availability(connected, curated_count, problem=''):
    """setup (nothing to show), curated (saved channels only), connected, or attention (discovery failed)."""
    if connected:
        return 'attention' if problem else 'connected'
    return 'curated' if curated_count else 'setup'


def twitch_note(state, message=''):
    if state == 'attention':
        return message or 'Twitch needs attention. Open Preferences to check the sign-in.'
    if state == 'curated':
        return CURATED_NOTE
    return SETUP_NOTE


def footnote(state, has_channels):
    """The line under the channel list, or None."""
    if state == 'curated':
        return CURATED_NOTE
    if state == 'connected' and not has_channels:
        return 'Nobody is live in the music or ASMR categories right now.'
    if state in ('unknown',):
        return 'Looking for live channels…'
    return None
