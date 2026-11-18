"""Preferences > Connections > Twitch > Review my follows.

Reads the channels you follow only when you press Review, then lists them to tick; nothing is
added to a style until you choose. `dialog` is the Preferences dialog (its toast, run and notice).
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk

import twitch_channels
from next_widgets import name_it


def build_follows(dialog):
    """Read the channels you follow, only when asked, and add the ones you tick to a style."""
    group = Adw.PreferencesGroup(title='Review my follows',
                                 description='Reads the channels you follow once, when you press Review. Nothing is added until you choose.')
    dialog.follow_name = Adw.EntryRow(title='Your Twitch name', show_apply_button=True)
    dialog.follow_name.connect('apply', lambda *_: review_follows(dialog))
    dialog.follow_style = Adw.ComboRow(title='Add to', model=Gtk.StringList.new(['Music channels', 'ASMR channels']))
    review = Gtk.Button(label='Review', valign=Gtk.Align.CENTER)
    review.add_css_class('suggested-action')
    review.connect('clicked', lambda *_: review_follows(dialog))
    name_it(review, 'Review my Twitch follows', None)
    dialog.follow_style.add_suffix(review)
    group.add(dialog.follow_name)
    group.add(dialog.follow_style)
    return group

def review_follows(dialog):
    profile = dialog.follow_name.get_text().strip()
    if not profile:
        dialog.connection_notice.set_subtitle('Enter your Twitch name to review who you follow.')
        return
    style = 'asmr' if dialog.follow_style.get_selected() == 1 else 'music'
    dialog.connection_notice.set_subtitle('Reading Twitch follows…')

    def show(result):
        if result.get('status') != 'follows':
            dialog.connection_notice.set_subtitle(result.get('message') or 'Twitch follows could not be read.')
            return
        dialog.connection_notice.set_subtitle('')
        follows_dialog(dialog, result, style)
    dialog.run(lambda: twitch_channels.import_following(profile), show, dialog.connection_notice)

def follows_dialog(dialog, result, style):
    """The follows to tick, as a page pushed inside Preferences."""
    page = Adw.PreferencesPage()
    group = Adw.PreferencesGroup(description=result.get('message') or 'Tick the channels that fit this style.')
    checks = []
    for item in result.get('channels') or []:
        row = Adw.ActionRow(title=item['name'], subtitle=item['login'])
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        name_it(check, 'Add ' + item['name'], None)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        group.add(row)
        checks.append((check, item['login']))
    if not checks:
        group.add(Adw.ActionRow(title='You follow no channels on this account'))
    page.add(group)
    actions = Adw.PreferencesGroup()
    add = Adw.ButtonRow(title='Add selected to ' + style)
    add.add_css_class('suggested-action')
    actions.add(add)
    page.add(actions)

    def chosen(*_):
        try:
            existing = twitch_channels.curated_channels(style)
            picked = [login for check, login in checks if check.get_active()]
            twitch_channels.save_preferences(style, list(dict.fromkeys(existing + picked)), twitch_channels.excluded_channels())
        except (OSError, ValueError) as error:
            dialog.connection_notice.set_subtitle(str(error))
            return
        for key, row in dialog.channel_rows.items():
            row.set_text(' '.join(twitch_channels.excluded_channels() if key == 'excluded' else twitch_channels.curated_channels(key)))
        dialog.pop_subpage()
        dialog.toast('Added to ' + style + ' channels')
    add.connect('activated', chosen)
    dialog.push_page(page, 'Review follows for ' + style)
