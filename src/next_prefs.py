"""Preferences for the next Listening Room window.

The module keeps two halves apart. The functions at the top turn saved settings,
stored check records and a few local files into the words and choices the dialog
shows, so wording can be tested without a display. The dialog below wires those
to widgets and to the commands that already exist: ``settings`` through the
session controller, the connection helpers in ``online_services`` and
``provider_auth``, the local "pause other players" preference in ``listening_hub``
and ``metadata.*`` for recordings. It never shows a secret, never logs one, and
never sends a command that starts or changes playback.
"""
import json
import os
from pathlib import Path
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import audio_devices
import listening_hub as hub
import next_model
from next_async import pool, run_async
import next_twitch
import next_widgets
import online_services as online
import provider_auth
import session_client
import twitch_channels

ORDER = ('jellyfin', 'twitch', 'music_assistant', 'audiomuse', 'listenbrainz', 'home_assistant')
NAMES = {'jellyfin': 'Jellyfin', 'twitch': 'Twitch', 'music_assistant': 'Music Assistant',
         'audiomuse': 'AudioMuse', 'listenbrainz': 'ListenBrainz', 'home_assistant': 'Home Assistant'}
INITIALS = {'jellyfin': 'JF', 'twitch': 'TW', 'music_assistant': 'MA', 'audiomuse': 'AM',
            'listenbrainz': 'LB', 'home_assistant': 'HA'}
# What each connection can actually do, in the listener's words.
CHIPS = {'jellyfin': ('Browse', 'Plays here', 'Open elsewhere'),
         'twitch': ('Browse', 'Plays here', 'Open elsewhere'),
         'music_assistant': ('Browse', 'Plays on speakers'),
         'audiomuse': ('Station mixes',),
         'listenbrainz': ('History',),
         'home_assistant': ('Room lighting',)}
# A successful check is the only way to say "connected"; these are its words.
WORKING = {'jellyfin': 'Signed in · library ready',
           'twitch': 'Signed in · renews on its own',
           'music_assistant': 'Speakers answer · playback not tested',
           'audiomuse': 'Web service answers · analysis not confirmed',
           'listenbrainz': 'Account access verified',
           'home_assistant': 'Scenes available · lighting not tested'}
NOT_SET = 'Not set up'
ADDED = 'Added · not checked yet'
SAVED_SIGN_IN = 'Saved sign-in · not checked yet'
SERVICE_NOTES = {
    'jellyfin': 'Browse albums, playlists and tracks, and play them in this room. Sign in with your Jellyfin account.',
    'twitch': 'Curated music and ASMR channels work without access; signing in adds discovery from the channels you follow.',
    'music_assistant': 'Browse its library and play on a speaker it controls. The desktop ambience mix stays here.',
    'audiomuse': 'Optional sonic programming from a library track. The check reads the web service only; worker and analysis readiness are separate.',
    'listenbrainz': 'Read your listening history and recommendations on request. Automatic scrobbling stays off.',
    'home_assistant': 'Link a lighting scene to a room. A scene without reported entities cannot be linked.',
}
RECORDINGS_NOTE = ('Note: reports and estimates are observations, not a review of the audio. Only a human review of the '
                   'exact recording is evidence, and it stays bound to the audio version it was made on.')

CSS = '''
.next .prefs-tile { border-radius: 10px; min-width: 34px; min-height: 34px;
  background: rgba(255,255,255,.10); font-weight: 700; font-size: 13px; }
.next .prefs-name { font-weight: 600; font-size: 15px; }
.next .cap-chip { background: rgba(255,255,255,.08); border-radius: 10px; padding: 2px 7px;
  font-size: 11px; color: rgba(255,255,255,.80); }
.next .status-dot { border-radius: 4px; min-width: 8px; min-height: 8px; }
.next .status-dot.ok { background: #7fd1a8; }
.next .status-dot.warn { background: #f2c98a; }
.next .status-dot.bad { background: #ffb4a2; }
.next .status-dot.off { background: rgba(255,255,255,.35); }
.next .prefs-note { color: rgba(255,255,255,.66); font-size: 13px; }
'''

# ---- what the rows say ------------------------------------------------------

def configured(service, values, environ=None):
    """Whether a service has enough saved settings to be attempted at all."""
    environ = os.environ if environ is None else environ
    values = values or {}
    if service == 'twitch':
        return bool(values.get('signed_in'))
    if service == 'listenbrainz':
        return bool(values.get('token_file') or values.get('username') or environ.get('LISTENBRAINZ_TOKEN'))
    return bool(values.get('endpoint'))


def connection_rows(settings, checks, twitch=None, environ=None):
    """One row per connection; "connected" only follows a stored successful check.

    ``twitch`` is what the private sign-in files say: ``connected`` (validated or
    just authorized), ``saved`` (a login exists, not checked yet), ``error`` or
    ``none``. It is kept separate because Twitch sign-in lives in its own file.
    """
    checks = checks if isinstance(checks, dict) else {}
    rows = []
    for service in ORDER:
        values = dict((settings or {}).get(service) or {})
        check = checks.get(service)
        check = check if isinstance(check, dict) else {}
        worked = check.get('state') == 'working'
        if service == 'twitch':
            state = (twitch or {}).get('state', 'none')
            configured_now = state in ('connected', 'saved')
            if state == 'error':
                status, tone = str((twitch or {}).get('detail') or 'The saved sign-in needs attention'), 'bad'
            elif state == 'connected':
                status, tone = WORKING[service], 'ok'
            elif state == 'saved':
                status, tone = SAVED_SIGN_IN, 'warn'
            else:
                status, tone = NOT_SET, 'off'
            worked = state == 'connected'
        else:
            configured_now = configured(service, values, environ)
            if worked:
                status, tone = WORKING[service], 'ok'
                if service == 'jellyfin' and not values.get('user_id'):
                    status = 'Connected · account ready'
            elif configured_now and check.get('error'):
                status, tone = str(check['error']), 'bad'
            elif configured_now:
                status, tone = ADDED, 'warn'
            else:
                status, tone = NOT_SET, 'off'
        rows.append({'id': service, 'name': NAMES[service], 'initials': INITIALS[service],
                     'chips': list(CHIPS[service]), 'status': status, 'tone': tone,
                     'configured': configured_now, 'working': worked,
                     'action': 'Manage' if configured_now else 'Set up'})
    return rows


def twitch_state():
    """Read the private Twitch sign-in without any request.

    ``connected`` is reserved for a login that succeeded here or was validated;
    a saved file alone is only ``saved``.
    """
    import twitch_credentials
    try:
        saved = twitch_credentials.load()
    except twitch_credentials.CredentialError as error:
        return {'state': 'error', 'detail': str(error)}
    return {'state': 'saved'} if saved.access_token else {'state': 'none'}


def youtube_status():
    """What YouTube search can do here, from whether yt-dlp is installed (no request is made)."""
    import youtube_search
    status = youtube_search.status()
    return {'text': status['text'], 'tone': 'ok' if status['available'] else 'bad'}


def output_choices(outputs, preferred):
    """Combo model, sink names and the selected index for the output row."""
    outputs = [item for item in outputs or [] if isinstance(item, dict) and isinstance(item.get('name'), str)]
    labels = ['System output'] + [item.get('description') or item['name'] for item in outputs]
    names = [None] + [item['name'] for item in outputs]
    return labels, names, names.index(preferred) if preferred in names else 0


def placement_words():
    return [label for _key, label in next_model.PLACEMENTS]


def placement_index(value):
    keys = [key for key, _label in next_model.PLACEMENTS]
    return keys.index(value) if value in keys else 0


def display_choices(monitors, preferred):
    monitors = [item for item in monitors or [] if isinstance(item, dict) and item.get('name')]
    labels = ['Current display'] + [item.get('description') or item['name'] for item in monitors]
    names = [None] + [item['name'] for item in monitors]
    return labels, names, names.index(preferred) if preferred in names else 0


def acoustid_status(ready, environ=None):
    """Where the AcoustID application key comes from, in plain words."""
    environ = os.environ if environ is None else environ
    if environ.get('ACOUSTID_CLIENT_KEY'):
        return 'Environment override in use (ACOUSTID_CLIENT_KEY)', 'warn'
    if ready:
        return 'Application key saved in a private file', 'ok'
    return 'No application key yet · matching by name still works', 'off'


def library_references(items, limit=20):
    """The Jellyfin audio tracks among ``items``, for a bulk job."""
    return [dict(item) for item in items or []
            if isinstance(item, dict) and item.get('provider') == 'jellyfin'
            and item.get('type') == 'Audio'][:limit]


def running_jobs(jobs):
    """Current metadata jobs, waiting or running; finished work is not listed."""
    rows = []
    for job in jobs or []:
        if not isinstance(job, dict) or job.get('status') not in ('queued', 'running'):
            continue
        detail = 'Running' if job.get('status') == 'running' else 'Waiting'
        if job.get('message'):
            detail += ' · ' + str(job['message'])
        rows.append({'id': job.get('job_id'), 'title': str(job.get('kind') or 'Metadata').capitalize(),
                     'detail': detail})
    return rows


def jobs_summary(jobs):
    count = len(running_jobs(jobs))
    if not count:
        return 'No metadata jobs.'
    return '1 metadata job in progress' if count == 1 else '%d metadata jobs in progress' % count


def export_document(pages):
    """Merge controller export pages into one file body."""
    records = []
    for page in pages or []:
        if isinstance(page, dict):
            records.extend(item for item in (page.get('records') or []) if isinstance(item, dict))
    return {'version': 1, 'records': records[:2000]}


def import_records(text):
    """Read a metadata export; controller limits stay in force per chunk."""
    if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
        raise ValueError('Choose a recording-notes file smaller than 2 MB')
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError('This file is not a recording-notes export') from None
    records = data.get('records') if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ValueError('This file is not a recording-notes export')
    return [item for item in records if isinstance(item, dict)]


def chunks(values, size=200):
    return [values[index:index + size] for index in range(0, len(values), size)]


def waybar_status(config_home=None):
    """Whether this user's Waybar configuration shows the room."""
    base = Path(config_home or os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'waybar'
    for name in ('config', 'config.jsonc', 'config.json'):
        try:
            text = base.joinpath(name).read_text(errors='ignore')[:200000]
        except OSError:
            continue
        if 'listening-roomctl status' in text or 'custom/listening' in text:
            return 'Shows the room and its layers in Waybar', 'ok'
        return 'Waybar is configured but has no Listening room module', 'warn'
    return 'No Waybar configuration found for this user', 'off'


def launcher_status(data_home=None, data_dirs=None):
    """Whether a desktop launcher entry exists for this user."""
    home = Path(data_home or os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    extra = data_dirs if data_dirs is not None else os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
    for base in [home] + [Path(item) for item in extra if item]:
        path = base / 'applications' / 'listening-room.desktop'
        try:
            text = path.read_text(errors='ignore')[:20000]
        except OSError:
            continue
        return ('Launcher entry installed · with room actions' if 'Actions=' in text
                else 'Launcher entry installed · no extra actions'), 'ok'
    return 'No launcher entry found for this user', 'off'


# ---- the dialog -------------------------------------------------------------

def _tile(initials):
    label = next_widgets.label(initials, ['prefs-tile'])
    label.set_size_request(34, 34)
    label.set_halign(Gtk.Align.CENTER)
    return label


def _dot(tone):
    dot = Gtk.Box(valign=Gtk.Align.CENTER)
    dot.add_css_class('status-dot')
    dot.add_css_class(tone)
    dot.set_size_request(8, 8)
    return dot


def _chip(text):
    return next_widgets.label(text, ['cap-chip'])


def paragraph(text):
    """A wrapped note inside a preferences list."""
    label = next_widgets.label(text, ['prefs-note'], wrap=True)
    label.set_margin_top(8)
    label.set_margin_bottom(8)
    label.set_margin_start(12)
    label.set_margin_end(12)
    label.set_selectable(True)
    row = Adw.PreferencesRow(activatable=False)
    row.set_child(label)
    row.set_activatable(False)
    return row


def button_row(title, label, callback, subtitle=None, suggested=False):
    """A row whose suffix button performs one explicit action."""
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=False)
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if suggested:
        button.add_css_class('suggested-action')
    button.connect('clicked', lambda *_: callback())
    row.add_suffix(button)
    row.button = button
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    """Sound and output, scene and motion, connections, phone and desktop, recordings."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.closed = False
        self.updating = False
        self.executor = pool('room-preferences', 4)
        self.cancel = threading.Event()
        self.twitch_login = None
        self.twitch_validated = False
        self.failures = {}
        self.jobs = []
        self.acoustid_ready = False
        self.phone_devices = []
        self.pages = {}
        self.set_content_width(700)
        self.set_content_height(600)
        next_widgets.install_css(window.get_display(), CSS, 2)   # shared with Track details
        self.pages['connections'] = self.build_connections()
        self.pages['sound'] = self.build_sound()
        self.pages['scene'] = self.build_scene()
        self.pages['phone'] = self.build_phone()
        self.pages['recordings'] = self.build_recordings()
        self.pages['about'] = self.build_about()
        for page in self.pages.values():
            self.add(page)
        self.connect('closed', lambda *_: self.cleanup())
        self.timer = GLib.timeout_add_seconds(3, self.refresh_recordings)
        GLib.idle_add(self.initial_state)

    # ---- lifecycle ----------------------------------------------------------

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.set()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.twitch_login is not None:
            self.twitch_login.cancel()
            self.twitch_login = None
        self.executor.shutdown(wait=False, cancel_futures=True)

    def toast(self, title, undo_label=None, undo=None):
        toast = Adw.Toast(title=title, timeout=5)
        if undo_label and undo:
            toast.set_button_label(undo_label)
            toast.connect('button-clicked', lambda *_: undo())
        self.add_toast(toast)

    def run(self, work, done=None, notice=None, failed=None):
        """Run one bounded operation off the interface thread."""
        if self.closed:
            return
        run_async(work, lambda result: self.finished(result, None, done, notice, failed),
                  # value-free wording for the row
                  lambda exc: self.finished(None, str(exc) or 'This did not work; check the connection and try again.',
                                            done, notice, failed),
                  self.executor)

    def finished(self, result, error, done, notice, failed=None):
        if self.closed:
            return False
        if error:
            if failed is not None:
                failed(error)
            if notice is not None:
                notice.set_subtitle(error)
            else:
                self.toast(error)
        elif done is not None:
            done(result)
        return False

    def settings(self):
        return dict(self.window.snapshot.get('settings') or {})

    def initial_state(self):
        if self.closed:
            return False
        self.refresh_connections()
        self.refresh_from_snapshot()
        self.load_outputs()
        self.refresh_recordings()
        return False

    # ---- Connections --------------------------------------------------------

    def build_connections(self):
        page = Adw.PreferencesPage(title='Connections', icon_name='emblem-synchronizing-symbolic')
        group = Adw.PreferencesGroup(
            title='More places to find music',
            )
        self.connection_widgets = {}
        for row in connection_rows({}, {}, None):
            group.add(self.build_connection_row(row))
        page.add(group)
        page.add(self.build_youtube())
        return page

    def build_youtube(self):
        """YouTube search needs no account, only yt-dlp; say which one is true on this system."""
        status = youtube_status()
        group = Adw.PreferencesGroup(title='YouTube')
        self.youtube_row = Adw.ActionRow(title='Search and play', subtitle=status['text'], activatable=False)
        self.youtube_row.add_prefix(_dot(status['tone']))
        group.add(self.youtube_row)
        return group

    def build_connection_row(self, row):
        """One connection: initials, name and truthful status, capability chips, one action."""
        grid = Gtk.Grid(column_spacing=12, row_spacing=2, margin_top=10, margin_bottom=10,
                        margin_start=6, margin_end=6)
        tile = _tile(row['initials'])
        tile.set_valign(Gtk.Align.CENTER)
        grid.attach(tile, 0, 0, 1, 2)
        name = next_widgets.label(row['name'], ['prefs-name'])
        name.set_hexpand(True)
        name.set_valign(Gtk.Align.END)
        grid.attach(name, 1, 0, 1, 1)
        chips = Gtk.Box(spacing=6, valign=Gtk.Align.END, halign=Gtk.Align.END)
        for text_value in row['chips']:
            chips.append(_chip(text_value))
        grid.attach(chips, 2, 0, 1, 1)
        status = Gtk.Box(spacing=8, valign=Gtk.Align.START)
        dot = _dot(row['tone'])
        status.append(dot)
        detail = next_widgets.label(row['status'], ['prefs-note'], ellipsize=True)
        status.append(detail)
        grid.attach(status, 1, 1, 2, 1)
        button = Gtk.Button(label=row['action'], valign=Gtk.Align.CENTER)
        button.connect('clicked', lambda _b, service=row['id']: self.open_connection(service))
        grid.attach(button, 3, 0, 1, 2)
        widget = Adw.PreferencesRow(activatable=False)
        widget.set_child(grid)
        widget.set_activatable(False)
        self.connection_widgets[row['id']] = {'detail': detail, 'dot': dot, 'button': button}
        return widget

    def refresh_connections(self):
        if self.closed:
            return
        settings = online.config()
        twitch = twitch_state()
        if self.twitch_validated and twitch['state'] in ('saved', 'none'):
            twitch = {'state': 'connected'}
        checks = dict(online.checks())
        for service, (signature, message, when) in list(self.failures.items()):
            stored = checks.get(service)
            newer = isinstance(stored, dict) and stored.get('state') == 'working' and stored.get('checked_at', 0) >= when
            if signature == self.signature(settings, service) and not newer:
                checks[service] = {'state': 'failed', 'error': message}
            else:
                del self.failures[service]
        for row in connection_rows(settings, checks, twitch):
            widgets = self.connection_widgets.get(row['id'])
            if not widgets:
                continue
            widgets['detail'].set_text(row['status'])
            widgets['button'].set_label(row['action'])
            dot = widgets['dot']
            for tone in ('ok', 'warn', 'bad', 'off'):
                dot.remove_css_class(tone)
            dot.add_css_class(row['tone'])

    def show_connection(self, service):
        """Jump to a connection's own page (used by Listen's "set up" entries)."""
        if service in NAMES:
            self.set_visible_page(self.pages['connections'])
            self.open_connection(service)

    def push_page(self, page, title):
        """A subpage with its own header.

        Inside a dialog, the navigation view adds the back button and the close
        button to that header, so both are reachable from the keyboard.
        """
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title))
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        nav = Adw.NavigationPage(title=title)
        nav.set_child(toolbar)
        self.push_subpage(nav)
        return nav

    @staticmethod
    def signature(settings, service):
        item = (settings or {}).get(service) or {}
        return (item.get('endpoint') or '', item.get('token_file') or '')

    def open_connection(self, service):
        """A pushed page with the setup fields and flows."""
        values = online.config().get(service) or {}
        page = Adw.PreferencesPage(title=NAMES[service])
        note = Adw.PreferencesGroup(description=SERVICE_NOTES[service])
        page.add(note)
        fields = {}
        connection = Adw.PreferencesGroup(title='Connection')
        if service == 'listenbrainz':
            connection.set_description('Uses https://api.listenbrainz.org; only the account token is needed.')
        else:
            endpoint = Adw.EntryRow(title='Server address')
            endpoint.set_text(values.get('endpoint') or '')
            endpoint.set_tooltip_text('The final HTTP or HTTPS address, including any installation subpath.')
            connection.add(endpoint)
            fields['endpoint'] = endpoint
        if service == 'jellyfin':
            fields['username'] = Adw.EntryRow(title='Jellyfin username')
            fields['password'] = Adw.PasswordEntryRow(title='Jellyfin password')
            connection.add(fields['username'])
            connection.add(fields['password'])
            connection.add(button_row('Sign in to Jellyfin', 'Sign in', self.jellyfin_sign_in, suggested=True))
        if service == 'twitch':
            fields['client_id'] = Adw.EntryRow(title='Twitch application client ID')
            connection.add(fields['client_id'])
            connection.add(button_row('Device sign-in', 'Begin sign-in', self.twitch_begin,
                                      subtitle='Twitch shows a short code; enter it on Twitch, then finish here.'))
            self.twitch_code = Adw.ActionRow(title='Sign-in code', subtitle='No sign-in has been started.', activatable=False)
            connection.add(self.twitch_code)
            connection.add(button_row('Finish sign-in', 'Finish', self.twitch_finish))
            connection.add(button_row('Check a saved sign-in', 'Check', self.twitch_check))
        page.add(connection)

        if service != 'twitch':
            secrets = Adw.PreferencesGroup(title='Private sign-in', description=(
                'Tokens stay in a private file owned by you, separate from room settings. '
                'An environment variable wins over a saved token. Values are never shown again.'))
            fields['token'] = Adw.PasswordEntryRow(title='Access token')
            fields['token'].set_tooltip_text('Leave blank to keep the saved token.')
            fields['token_file'] = Adw.EntryRow(title='Private token file')
            fields['token_file'].set_text(values.get('token_file') or '')
            secrets.add(fields['token'])
            secrets.add(fields['token_file'])
            page.add(secrets)

        actions = Adw.PreferencesGroup(title='Check this connection' if service != 'twitch' else 'Status')
        self.connection_notice = Adw.ActionRow(title='Status', subtitle='Not checked yet.', activatable=False)
        actions.add(self.connection_notice)
        if service != 'twitch':
            actions.add(button_row('Save these settings', 'Save', lambda: self.save_connection(service, fields)))
            actions.add(button_row('Test the connection', 'Test', lambda: self.test_connection(service, fields),
                                   subtitle='A test opens nothing and plays nothing.'))
        page.add(actions)
        if service == 'twitch':
            page.add(self.build_twitch_channels())
            page.add(next_twitch.build_follows(self))
        if service == 'home_assistant':
            page.add(self.build_lighting())
        self.connection_fields = fields
        self.push_page(page, NAMES[service])

    def build_twitch_channels(self):
        """The channels Listen › Live channels offers without signing in, and the ones never suggested."""
        group = Adw.PreferencesGroup(title='Channels you keep',
                                     description='Names or twitch.tv links, separated by spaces or commas. Applied when you press Enter.')
        self.channel_rows = {}
        try:
            current = {'music': twitch_channels.curated_channels('music'), 'asmr': twitch_channels.curated_channels('asmr'),
                       'excluded': twitch_channels.excluded_channels()}
        except (ValueError, OSError):
            current = {'music': [], 'asmr': [], 'excluded': []}
        for key, title in (('music', 'Music channels'), ('asmr', 'ASMR channels'), ('excluded', 'Never suggest')):
            row = Adw.EntryRow(title=title, show_apply_button=True)
            row.set_text(' '.join(current[key]))
            row.connect('apply', lambda _row, key=key: self.apply_channels(key))
            group.add(row)
            self.channel_rows[key] = row
        return group

    def apply_channels(self, key):
        """Save one list; Undo restores what was there. Saving never starts playback."""
        rows = self.channel_rows
        try:
            values = {name: twitch_channels.parse_names(row.get_text()) for name, row in rows.items()}
            before = {'music': twitch_channels.curated_channels('music'), 'asmr': twitch_channels.curated_channels('asmr'),
                      'excluded': twitch_channels.excluded_channels()}
            style = 'asmr' if key == 'asmr' else 'music'
            twitch_channels.save_preferences(style, values[style], values['excluded'])
        except (ValueError, OSError) as error:
            self.connection_notice.set_subtitle(str(error))
            return

        def undo():
            twitch_channels.save_preferences(style, before[style], before['excluded'])
            for name, row in rows.items():
                row.set_text(' '.join(before[name]))
        self.toast('Channels saved', 'Undo', undo)

    def save_connection(self, service, fields, quiet=False):
        try:
            values = {'token_file': fields['token_file'].get_text()}
            if 'endpoint' in fields:
                values['endpoint'] = fields['endpoint'].get_text()
            online.save_connection(service, values)
            secret = fields['token'].get_text()
            if secret:
                online.save_token(service, secret)
                fields['token'].set_text('')
                fields['token_file'].set_text(online.config()[service]['token_file'])
        except online.ConnectionError as error:
            self.connection_notice.set_subtitle(str(error))
            return False
        self.connection_notice.set_subtitle('Saved. Test the connection to see what it can do.')
        if not quiet:
            self.toast(NAMES[service] + ' settings saved')
        self.refresh_connections()
        return True

    def test_connection(self, service, fields):
        if not self.save_connection(service, fields, quiet=True):
            return
        self.connection_notice.set_subtitle('Testing…')
        self.failures.pop(service, None)
        tested = self.signature(online.config(), service)

        def failed(message):
            self.failures[service] = (tested, message, int(time.time()))
            self.refresh_connections()

        def show(result):
            states = ', '.join(key.replace('_', ' ') + ': ' + value for key, value in result['capabilities'].items())
            self.connection_notice.set_subtitle(result['detail'] + ' (' + states + ')')
            self.toast(NAMES[service] + ' check finished')
            self.refresh_connections()
        self.run(lambda: online.test_connection(service, self.cancel), show, self.connection_notice, failed)

    def jellyfin_sign_in(self):
        fields = self.connection_fields
        if not self.save_connection('jellyfin', fields, quiet=True):
            return
        user, secret = fields['username'].get_text(), fields['password'].get_text()
        fields['password'].set_text('')
        if not user or not secret:
            self.connection_notice.set_subtitle('Enter the Jellyfin username and password first.')
            return

        def done(_result):
            fields['token_file'].set_text(online.config()['jellyfin']['token_file'])
            self.connection_notice.set_subtitle('Signed in. Test the connection to verify library access.')
            self.refresh_connections()
        self.run(lambda: online.jellyfin_login(user, secret, self.cancel), done, self.connection_notice)

    def twitch_begin(self):
        try:
            login = provider_auth.TwitchDeviceLogin(self.connection_fields['client_id'].get_text().strip())
        except provider_auth.AuthError as error:
            self.connection_notice.set_subtitle(str(error))
            return
        if self.twitch_login is not None:
            self.twitch_login.cancel()
        self.twitch_login = login
        self.twitch_code.set_subtitle('Waiting for Twitch…')

        def show(result):
            self.twitch_code.set_subtitle('Enter ' + result['code'] + ' at twitch.tv/activate, then choose Finish.')
        self.run(lambda: login.begin(), show, self.connection_notice)

    def twitch_finish(self):
        login = self.twitch_login
        if login is None:
            self.connection_notice.set_subtitle('Begin sign-in first.')
            return

        def show(result):
            if result.get('connected'):
                self.twitch_login = None
                self.twitch_validated = True
                self.twitch_code.set_subtitle('Signed in. Twitch discovery can use this account.')
                self.connection_notice.set_subtitle('Connected.')
                self.refresh_connections()
            else:
                self.connection_notice.set_subtitle('Waiting for approval. Approve on Twitch, then choose Finish again.')
        self.run(lambda: login.finish(), show, self.connection_notice)

    def twitch_check(self):
        def show(result):
            if result.get('state') == 'connected':
                self.twitch_validated = True
                self.connection_notice.set_subtitle('Saved sign-in validated for discovery.')
            else:
                self.connection_notice.set_subtitle('No saved Twitch sign-in yet. Begin sign-in first.')
            self.refresh_connections()
        self.run(provider_auth.validate_saved_twitch, show, self.connection_notice)

    def build_lighting(self):
        """Link a Home Assistant scene to a room."""
        import room_catalogue
        self.lighting_scenes = []
        self.lighting_rooms = [{'id': room['id'], 'name': room['name']} for room in room_catalogue.RECIPES]
        lighting = Adw.PreferencesGroup(title='Room lighting', description=(
            'Link a scene to a room. Entering that room can then apply it, and leaving restores the lighting it '
            'replaced. Linking never changes a light now.'))
        self.lighting_room = Adw.ComboRow(title='Room',
                                          model=Gtk.StringList.new([room['name'] for room in self.lighting_rooms]))
        self.lighting_scene = Adw.ComboRow(title='Scene', model=Gtk.StringList.new(['Load scenes first']))
        lighting.add(self.lighting_room)
        lighting.add(self.lighting_scene)
        lighting.add(button_row('Lighting scenes', 'Load scenes', self.load_scenes,
                                subtitle='Reads scene names and their entities; no light is changed.'))
        lighting.add(button_row('Link this scene to the room', 'Link', self.link_lighting,
                                subtitle='A scene that does not report its entities cannot be linked.'))
        lighting.add(button_row('Remove the room’s scene link', 'Remove', self.unlink_lighting))
        return lighting

    def load_scenes(self):
        self.connection_notice.set_subtitle('Loading lighting scenes…')

        def show(rows):
            self.lighting_scenes = [dict(row) for row in rows or [] if isinstance(row, dict) and row.get('id')]
            self.lighting_scene.set_model(Gtk.StringList.new(
                [row.get('name') or 'Scene' for row in self.lighting_scenes] or ['No scenes found']))
            self.connection_notice.set_subtitle('%d scenes returned. No light was changed.' % len(self.lighting_scenes))
        self.run(lambda: online.ha_scenes(self.cancel), show, self.connection_notice)

    def link_lighting(self):
        index, room_index = self.lighting_scene.get_selected(), self.lighting_room.get_selected()
        if index >= len(self.lighting_scenes) or room_index >= len(self.lighting_rooms):
            self.connection_notice.set_subtitle('Load scenes and choose a room first.')
            return
        scene, room = self.lighting_scenes[index], self.lighting_rooms[room_index]
        if not scene.get('entities'):
            self.connection_notice.set_subtitle(
                'This scene does not report its affected entities, so the previous lighting cannot be captured. '
                'Choose another scene.')
            return
        try:
            online.set_room_scene(room['id'], scene['id'], scene['entities'])
        except online.ConnectionError as error:
            self.connection_notice.set_subtitle(str(error))
            return
        self.connection_notice.set_subtitle(scene.get('name') + ' will apply when entering ' + room['name'] + '.')
        self.toast('Lighting linked to ' + room['name'])

    def unlink_lighting(self):
        room_index = self.lighting_room.get_selected()
        if room_index >= len(self.lighting_rooms):
            return
        room = self.lighting_rooms[room_index]
        try:
            online.set_room_scene(room['id'])
        except online.ConnectionError as error:
            self.connection_notice.set_subtitle(str(error))
            return
        self.connection_notice.set_subtitle('Removed the lighting link for ' + room['name'] + '.')

    # ---- Sound and output ---------------------------------------------------

    def build_sound(self):
        page = Adw.PreferencesPage(title='Sound and output', icon_name='audio-speakers-symbolic')
        group = Adw.PreferencesGroup(title='Output', description=(
            'Where the room plays. Other apps keep their own output; the system default is never changed.'))
        self.output_row = Adw.ComboRow(title='Output', model=Gtk.StringList.new(['System output']))
        self.output_row.set_subtitle('Loading outputs…')
        self.output_row.connect('notify::selected', self.output_chosen)
        group.add(self.output_row)
        group.add(button_row('Test sound', 'Play test', self.test_sound,
                             subtitle='One quiet second on the chosen output, only when you ask.'))
        page.add(group)
        policies = Adw.PreferencesGroup(title='During a session')
        self.pause_switch = Adw.SwitchRow(title='Pause if it disconnects',
                                          subtitle='Stop the room if the chosen output goes away.')
        self.pause_switch.connect('notify::active', lambda *_: self.switch_changed(
            'pause_on_disconnect', self.pause_switch.get_active(), 'Pause if it disconnects'))
        self.duck_switch = Adw.SwitchRow(title='Lower room audio during calls',
                                         subtitle='Only applications that declare a call or phone role count.')
        self.duck_switch.connect('notify::active', lambda *_: self.switch_changed(
            'duck_on_call', self.duck_switch.get_active(), 'Lower room audio during calls'))
        policies.add(self.pause_switch)
        policies.add(self.duck_switch)
        self.others_switch = Adw.SwitchRow(title='Pause other players when a room starts',
                                          subtitle='Other desktop players pause while this room plays.')
        self.others_switch.connect('notify::active', lambda *_: self.others_changed())
        policies.add(self.others_switch)
        page.add(policies)
        return page

    def load_outputs(self):
        self.run(audio_devices.outputs, self.show_outputs, None)

    def show_outputs(self, outputs):
        preferred = self.settings().get('preferred_output')
        labels, names, selected = output_choices(outputs, preferred)
        self.output_names = names
        self.updating = True
        self.output_row.set_model(Gtk.StringList.new(labels))
        self.output_row.set_selected(selected)
        self.output_row.set_subtitle('Owned room audio only.' if names[1:] else 'No separate outputs found; the system output is used.')
        self.updating = False

    def output_chosen(self, *_):
        if self.updating or not getattr(self, 'output_names', None):
            return
        index = self.output_row.get_selected()
        if index >= len(self.output_names):
            return
        name = self.output_names[index]
        previous = self.settings().get('preferred_output')
        text = self.output_row.get_model().get_string(index)
        self.window.command('settings', {'values': {'preferred_output': name}},
                            done=lambda _r: self.toast('Output: ' + text, 'Undo',
                                                      lambda: self.apply_settings({'preferred_output': previous},
                                                                                  'Output restored', None)))

    def test_sound(self):
        preferred = self.settings().get('preferred_output')
        self.run(lambda: audio_devices.test_sound(preferred),
                 lambda _result: self.toast('Played a quiet test sound'), None)

    def switch_changed(self, key, value, title):
        if self.updating:
            return
        previous = self.settings().get(key)
        if previous == value:
            return
        self.apply_settings({key: value}, title + ': ' + ('on' if value else 'off'), {key: previous})
        if key == 'reduced_motion':
            self.sync_motion_rows(value)

    def others_changed(self):
        if self.updating:
            return
        value = self.others_switch.get_active()
        previous = bool(hub.state().get('pause_others_on_radio_start', False))
        if previous == value:
            return

        def work():
            hub.update_state(pause_others_on_radio_start=value)
            return value
        self.run(work, lambda _r: self.toast(
            'Other players pause when a room starts' if value else 'Other players keep playing', 'Undo',
            lambda: self.run(lambda: hub.update_state(pause_others_on_radio_start=previous), None, None)),
            None)

    def apply_settings(self, values, message, undo_values):
        # An Undo says so ('undo': True among the values): putting the previous value back is not a new choice,
        # so undoing a placement change does not count as choosing a placement (O9).
        self.window.command('settings', {'values': values},
                            done=lambda _r: self.toast(
                                message, 'Undo' if undo_values is not None else None,
                                (lambda: self.window.command('settings', {'values': dict(undo_values, undo=True)}))
                                if undo_values else None))

    # ---- Scene and motion ---------------------------------------------------

    def build_scene(self):
        page = Adw.PreferencesPage(title='Scene and motion', icon_name='applications-graphics-symbolic')
        scene = Adw.PreferencesGroup(title='New rooms', description=(
            'Defaults for the scene each room opens with. A room change keeps this session’s own choices.'))
        self.placement_row = Adw.ComboRow(title='Default placement', model=Gtk.StringList.new(placement_words()))
        if not next_model.hyprland():
            self.placement_row.set_subtitle(next_model.NO_HYPRLAND)
        self.placement_row.connect('notify::selected', self.placement_chosen)
        scene.add(self.placement_row)
        self.display_row = Adw.ComboRow(title='Preferred display', model=Gtk.StringList.new(['Current display']))
        self.display_row.connect('notify::selected', self.display_chosen)
        scene.add(self.display_row)
        page.add(scene)
        motion = Adw.PreferencesGroup(title='Motion')
        self.moving_switch = Adw.SwitchRow(title='Moving picture',
                                          subtitle='Play the reviewed scene loop instead of still art.')
        self.moving_switch.connect('notify::active', lambda *_: self.moving_changed())
        self.reduced_switch = Adw.SwitchRow(title='Reduced motion',
                                            subtitle='Still scenes and no animation. Audio fades stay.')
        self.reduced_switch.connect('notify::active', lambda *_: self.switch_changed(
            'reduced_motion', self.reduced_switch.get_active(), 'Reduced motion'))
        motion.add(self.moving_switch)
        motion.add(self.reduced_switch)
        page.add(motion)
        controls = Adw.PreferencesGroup(title='Controls', description=(
            'The controls in Now fade after a few seconds without input.'))
        self.visible_switch = Adw.SwitchRow(title='Keep controls visible',
                                           subtitle='Never fade the controls in Now.')
        self.visible_switch.connect('notify::active', lambda *_: self.switch_changed(
            'keep_controls', self.visible_switch.get_active(), 'Keep controls visible'))
        controls.add(self.visible_switch)
        page.add(controls)
        return page

    def refresh_from_snapshot(self):
        if self.closed:
            return
        settings = self.settings()
        self.updating = True
        try:
            self.placement_row.set_selected(placement_index(settings.get('placement', '9')))
            self.moving_switch.set_active(not settings.get('still', False))
            self.reduced_switch.set_active(bool(settings.get('reduced_motion')))
            self.visible_switch.set_active(bool(settings.get('keep_controls')))
            self.pause_switch.set_active(bool(settings.get('pause_on_disconnect')))
            self.duck_switch.set_active(bool(settings.get('duck_on_call')))
            self.others_switch.set_active(bool(hub.state().get('pause_others_on_radio_start', False)))
        finally:
            self.updating = False
        self.sync_motion_rows()
        self.load_displays()

    def sync_motion_rows(self, reduced=None):
        """A moving picture is impossible under reduced motion, and the row says so."""
        reduced = bool(self.settings().get('reduced_motion')) if reduced is None else bool(reduced)
        self.moving_switch.set_sensitive(not reduced)
        self.moving_switch.set_subtitle('Off while reduced motion is on.' if reduced
                                       else 'Play the reviewed scene loop instead of still art.')

    def load_displays(self):
        import ambience
        self.run(ambience.monitors, self.show_displays, None)

    def show_displays(self, monitors):
        labels, names, selected = display_choices(monitors, self.settings().get('preferred_display'))
        self.display_names = names
        self.updating = True
        self.display_row.set_model(Gtk.StringList.new(labels))
        self.display_row.set_selected(selected)
        self.display_row.set_sensitive(len(names) > 1)
        self.display_row.set_subtitle('' if len(names) > 1 else 'No extra displays found; the current one is used.')
        self.updating = False

    def placement_chosen(self, *_):
        if self.updating:
            return
        index = self.placement_row.get_selected()
        keys = [key for key, _label in next_model.PLACEMENTS]
        if index >= len(keys):
            return
        previous = self.settings().get('placement', '9')
        self.apply_settings({'placement': keys[index]}, 'Default placement: ' + placement_words()[index],
                            {'placement': previous})

    def display_chosen(self, *_):
        if self.updating or not getattr(self, 'display_names', None):
            return
        index = self.display_row.get_selected()
        if index >= len(self.display_names):
            return
        previous = self.settings().get('preferred_display')
        self.apply_settings({'preferred_display': self.display_names[index]}, 'Preferred display saved',
                            {'preferred_display': previous})

    def moving_changed(self):
        if self.updating:
            return
        still = not self.moving_switch.get_active()
        previous = bool(self.settings().get('still', False))
        if previous == still:
            return
        self.apply_settings({'still': still}, 'Moving picture ' + ('off' if still else 'on'), {'still': previous})

    # ---- Phone and desktop --------------------------------------------------

    def build_phone(self):
        page = Adw.PreferencesPage(title='Phone and desktop', icon_name='phone-symbolic')
        phone = Adw.PreferencesGroup(title='KDE Connect', description=(
            'Add named room commands to a paired phone. Existing unrelated commands are kept.'))
        self.phone_row = Adw.ComboRow(title='Paired phone', model=Gtk.StringList.new(['Find paired phones first']))
        phone.add(self.phone_row)
        phone.add(button_row('Find paired phones', 'Find', self.find_phones,
                             subtitle='Lists phones that are paired and reachable now.'))
        self.phone_notice = Adw.ActionRow(title='Room commands', subtitle='Not installed yet.', activatable=False)
        phone.add(self.phone_notice)
        phone.add(button_row('Install room commands', 'Install', self.install_phone,
                             subtitle='Adds room commands only; it never pairs a phone or changes firewall rules.'))
        page.add(phone)
        desktop = Adw.PreferencesGroup(title='Desktop', description='What this session can reach from the panel and the launcher.')
        self.waybar_row = self.status_row('Waybar', *waybar_status())
        self.launcher_row = self.status_row('Launcher', *launcher_status())
        desktop.add(self.waybar_row)
        desktop.add(self.launcher_row)
        page.add(desktop)
        return page

    def status_row(self, title, status, tone):
        row = Adw.ActionRow(title=title, subtitle=status, activatable=False)
        row.add_prefix(_dot(tone))
        return row

    def find_phones(self):
        self.phone_notice.set_subtitle('Looking for paired phones…')

        def show(rows):
            self.phone_devices = [dict(item) for item in rows or [] if isinstance(item, dict) and item.get('id')]
            self.phone_row.set_model(Gtk.StringList.new([item.get('name') or 'Phone' for item in self.phone_devices]
                                                        or ['No paired phones reachable']))
            self.phone_notice.set_subtitle(
                ('%d paired phones reachable. Choose one to install commands.' % len(self.phone_devices))
                if self.phone_devices else 'No paired phones are reachable right now.')
        self.run(online.paired_devices, show, self.phone_notice)

    def install_phone(self):
        index = self.phone_row.get_selected()
        if index >= len(self.phone_devices):
            self.phone_notice.set_subtitle('Find and choose a paired phone first.')
            return
        device = self.phone_devices[index]
        self.phone_notice.set_subtitle('Installing room commands…')

        def show(result):
            self.phone_notice.set_subtitle(
                str(result.get('installed', 0)) + ' room commands installed for ' + (device.get('name') or 'this phone')
                + ('. Open Run commands on the phone.' if result.get('notified')
                   else '. Reopen KDE Connect settings to reload them.'))
            self.toast('Room commands installed for ' + (device.get('name') or 'the phone'))
        self.run(lambda: online.install_remote_commands(device['id']), show, self.phone_notice)

    # ---- Recordings ---------------------------------------------------------

    def build_recordings(self):
        page = Adw.PreferencesPage(title='Recordings', icon_name='media-optical-symbolic')
        self.acoustid_row = Adw.ActionRow(title='AcoustID application key',
                                          subtitle=acoustid_status(False)[0], activatable=False)
        self.acoustid_dot = _dot(acoustid_status(False)[1])
        self.acoustid_row.add_prefix(self.acoustid_dot)
        keys = Adw.PreferencesGroup(title='AcoustID')
        keys.add(self.acoustid_row)
        self.acoustid_key = Adw.PasswordEntryRow(title='Application key')
        self.acoustid_key.set_tooltip_text('Leave blank to keep the saved key.')
        keys.add(self.acoustid_key)
        keys.add(button_row('Save the application key', 'Save', self.save_acoustid))
        keys.add(button_row('Remove the saved key', 'Remove', lambda: self.save_acoustid(clear=True),
                            subtitle='Matching by name still works without a key.'))
        page.add(keys)

        bulk = Adw.PreferencesGroup(title='Enrich in bulk', description=(
            'Up to 20 tracks are queued at a time. Jobs run in the background and never start music.'))
        self.upcoming_row = button_row('Upcoming tracks', 'Enrich', self.enrich_upcoming,
                                       subtitle='Tracks this station will play next.')
        self.queue_row = button_row('Prepared library queue', 'Enrich', self.enrich_queue,
                                    subtitle='The album or playlist currently prepared for this room.')
        bulk.add(self.upcoming_row)
        bulk.add(self.queue_row)
        page.add(bulk)

        self.jobs_group = Adw.PreferencesGroup(title='Jobs', description=jobs_summary([]))
        self.jobs_rows = []
        page.add(self.jobs_group)

        files = Adw.PreferencesGroup(title='Export and import', description=(
            'Exports carry provenance. Imports never overwrite local evidence or reviews, and imported reviews are '
            'not trusted.'))
        files.add(button_row('Export recording notes', 'Export…', self.export_notes))
        files.add(button_row('Import recording notes', 'Import…', self.import_notes))
        files.add(paragraph(RECORDINGS_NOTE))
        page.add(files)
        return page

    def save_acoustid(self, clear=False):
        value = '' if clear else self.acoustid_key.get_text().strip()
        if not clear and not value:
            self.toast('Enter the AcoustID application key first.')
            return
        self.acoustid_key.set_text('')
        self.window.command('metadata.configure_acoustid', {'client_key': value},
                            done=lambda result: self.show_acoustid(result.get('acoustid_ready'), announce=True))

    def show_acoustid(self, ready, announce=False):
        self.acoustid_ready = bool(ready)
        status, tone = acoustid_status(self.acoustid_ready)
        self.acoustid_row.set_subtitle(status)
        for name in ('ok', 'warn', 'bad', 'off'):
            self.acoustid_dot.remove_css_class(name)
        self.acoustid_dot.add_css_class(tone)
        if announce:
            self.toast('AcoustID key ' + ('saved' if self.acoustid_ready else 'removed'))

    def refresh_recordings(self):
        if self.closed:
            return False

        def work():
            return (session_client.request('metadata.status', {}, timeout=3),
                    session_client.request('metadata.jobs', {}, timeout=3))

        self.run(work, self.show_recordings, None)
        return True

    def show_recordings(self, result):
        status, jobs = result
        self.show_acoustid(status.get('acoustid_ready'))
        self.jobs = [job for job in (jobs.get('jobs') or []) if isinstance(job, dict)]
        self.render_jobs()

    def render_jobs(self):
        for row in self.jobs_rows:
            self.jobs_group.remove(row)
        self.jobs_rows = []
        rows = running_jobs(self.jobs)
        self.jobs_group.set_description(jobs_summary(self.jobs))
        for job in rows:
            row = button_row(job['title'], 'Cancel', lambda job_id=job['id']: self.cancel_job(job_id),
                             subtitle=job['detail'])
            self.jobs_group.add(row)
            self.jobs_rows.append(row)

    def cancel_job(self, job_id):
        def work():
            return session_client.request('metadata.cancel', {'job_id': job_id}, timeout=3)

        def cancelled(_result):
            self.toast('Cancelling that job')
            self.refresh_recordings()
        self.run(work, cancelled, None)

    def station_upcoming(self):
        return library_references((self.window.snapshot.get('station') or {}).get('upcoming'))

    def queue_items(self):
        return library_references(online.library_queue().get('items'))

    def enrich_upcoming(self):
        items = self.station_upcoming()
        if not items:
            self.toast('No upcoming library tracks to enrich')
            return
        self.enqueue(items)

    def enrich_queue(self):
        items = self.queue_items()
        if not items:
            self.toast('No prepared library queue to enrich')
            return
        self.enqueue(items)

    def enqueue(self, items):
        def work():
            accepted = 0
            for item in items[:20]:
                try:
                    session_client.request('metadata.enrich', {'reference': item}, timeout=5)
                    accepted += 1
                except (ValueError, OSError):
                    break
            return accepted

        def queued(count):
            self.toast('Queued %d of %d tracks' % (count, len(items)))
            self.refresh_recordings()
        self.run(work, queued, None)

    def export_notes(self):
        dialog = Gtk.FileDialog(title='Export recording notes')
        dialog.set_initial_name('recording-notes.json')
        dialog.save(self.window, None, self.export_chosen)

    def export_chosen(self, dialog, result):
        try:
            path = dialog.save_finish(result).get_path()
        except GLib.Error:
            return
        if not path:
            return

        def work():
            pages, cursor = [], None
            for _page in range(40):
                page = session_client.request('metadata.export', {'cursor': cursor, 'limit': 200}, timeout=5)
                pages.append(page)
                cursor = page.get('next_cursor')
                if not cursor:
                    break
            document = export_document(pages)
            Path(path).write_text(json.dumps(document, indent=2) + '\n')
            return len(document['records'])
        self.run(work, lambda count: self.toast('Exported %d recordings' % count), None)

    def import_notes(self):
        dialog = Gtk.FileDialog(title='Import recording notes')
        dialog.open(self.window, None, self.import_chosen)

    def import_chosen(self, dialog, result):
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        if not path:
            return

        def work():
            records = import_records(Path(path).read_text(errors='replace')[:2 * 1024 * 1024 + 1])
            imported = 0
            for batch in chunks(records):
                answer = session_client.request('metadata.import', {'records': batch}, timeout=5)
                imported += int(answer.get('imported') or 0)
            return imported

        def imported(count):
            self.toast('Imported %d recordings' % count)
            self.refresh_recordings()
        self.run(work, imported, None)

    # ---- About --------------------------------------------------------------

    def build_about(self):
        page = Adw.PreferencesPage(title='About', icon_name='help-about-symbolic')
        group = Adw.PreferencesGroup(title='Listening room', description='A quiet room for the radio and sounds you keep on.')
        group.add(Adw.ActionRow(title='Rooms, stations and recording notes',
                                subtitle='Saved on this computer, in your private state directory.', activatable=False))
        group.add(Adw.ActionRow(title='Account connections',
                                subtitle='Tokens stay in private files and are never shown again.', activatable=False))
        group.add(Adw.ActionRow(title='Browsing and previews',
                                subtitle='Nothing plays until you choose Tune in or Enter room.', activatable=False))
        page.add(group)
        return page
