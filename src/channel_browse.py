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


# ---- live channel rows, chips and ordering ---------------------------------------------

SIGNED_OUT_NOTE = 'Sign in to Twitch to see who’s live. Your channels still play.'
SORTS = (('most', 'Most watched'), ('fewest', 'Fewest watched'), ('yours', 'Your channels first'))


def viewers_text(count):
    """"845 watching", "1.2 k watching" (a count that is not known says nothing)."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return ''
    if count < 0:
        return ''
    if count >= 1000:
        thousands = count / 1000
        return ('%d k' % thousands if thousands >= 100 else ('%.1f k' % thousands).replace('.0 k', ' k')) + ' watching'
    return '%d watching' % count


def channel_detail(channel):
    """"Music · 1.2 k watching · EN" for a live channel; "Offline" or the plain "Live channel · Twitch" when
    its status is not known."""
    live = channel.get('live')
    category = str(channel.get('category') or '')
    if live is False:
        return ' · '.join(part for part in (category, 'Offline') if part)
    if live is None:
        return ' · '.join(part for part in (category or 'Live channel', 'Twitch'))
    return ' · '.join(part for part in (category or 'Live channel', viewers_text(channel.get('viewers')),
                                        str(channel.get('language') or '').upper(), '' if category else 'Twitch') if part)


def arrange(channels, sort='most'):
    """Channels in the chosen order, never dropping one. Live ones come before offline ones in every order.

    most:   viewers, high to low (Helix's own order for equal counts);
    fewest: viewers, low to high, to find the smaller channels;
    yours:  the listener's own (curated or followed) channels first, then by viewers."""
    def viewers(channel):
        try:
            return int(channel.get('viewers') or 0)
        except (TypeError, ValueError):
            return 0
    live = [c for c in channels if c.get('live') is not False]
    offline = [c for c in channels if c.get('live') is False]
    if sort == 'fewest':
        live = sorted(live, key=viewers)
    elif sort == 'yours':
        live = sorted(live, key=lambda c: (not c.get('curated'), -viewers(c)))
    else:
        live = sorted(live, key=lambda c: -viewers(c))
    return live + offline


def interleave(groups):
    """One list from several lists, taking turns, so a short shelf shows every category."""
    found, seen = [], set()
    for position in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if position < len(group) and group[position].get('login') not in seen:
                seen.add(group[position].get('login'))
                found.append(group[position])
    return found
