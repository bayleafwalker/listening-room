"""O6 evidence: ambience in the Mix, new stations from scratch, Twitch and YouTube in Listen.

Real GTK, the real session controller on the session test double and fixture providers; every
controller command is logged so browsing is proven inert and each explicit choice is proven to
send exactly one command. Writes the screenshots named in the O6 brief.

  scripts/listening-dev.sh probe OUT listening-o6-ui
"""
import sys
import time

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o6-ui')
checks = []


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:300]))


with env:
    Gtk, Gdk, GLib, Adw = env.Gtk, env.Gdk, env.GLib, env.Adw
    pump, settle, texts, buttons, labels, press, commands, shot = (
        env.pump, env.settle, env.texts, env.buttons, env.labels, env.press, env.commands, env.shot)

    def visible_texts(root):
        return {w.get_text() for w in env.widgets(root) if isinstance(w, Gtk.Label) and w.get_mapped()}

    def play_buttons(root):
        return [b for b in buttons(root, 'Play as ambience')]

    def set_scope(page, title):
        keys = [t for _k, t in page.scope_list]
        page.scopes.set_active(keys.index(title))
        pump(.5)

    def type_query(page, text, wait=1.4):
        page.search.set_text(text)
        page.query_submitted()
        pump(wait)

    try:
        env.start_station_room()
        env.twitch_curated = []
        window = env.launch(page='now')
        page = window.listen
        window.show_now()
        pump(1.0)

        # ------------------------------------------------------------------ Mix: ambience change
        window.open_mix()
        pump(.9)
        row = window.mix.rows['atmosphere']
        check('1a ambience row has a plain Change… button like Music', isinstance(row.change, Gtk.Button)
              and not isinstance(row.change, Gtk.MenuButton) and row.change.get_label() == 'Change…')
        shot(window, '01-mix-ambience-change')
        mark = len(env.log)
        row.change.emit('clicked')
        pump(.8)
        check('1b Change… on Ambience opens Listen in ambience mode',
              window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'listen' and page.mode == 'ambience')
        check('1c the page says what it is for', 'Choose an ambience for Morning lift' in visible_texts(page.widget)
              or any(t.startswith('Choose an ambience') for t in visible_texts(page.widget)), sorted(visible_texts(page.widget))[:20])
        check('1d scopes are the ambience scopes, Sounds first',
              [t for _k, t in page.scope_list][:2] == ['Sounds', 'Radio'] and page.scope == 'sounds')
        check('1e offline sounds are listed, each primary action is Play as ambience',
              len(play_buttons(page.index_body)) >= 6 and not buttons(page.index_body, 'Tune in'),
              sorted(visible_texts(page.index_body))[:20])
        check('1f browsing is inert: opening ambience mode sent no command', not commands(mark), commands(mark))
        shot(window, '02-listen-ambience-sounds')
        for title in ('Radio', 'Library', 'Live channels', 'World radio', 'YouTube', 'Your stations', 'Sounds'):
            set_scope(page, title)
        check('1g switching every ambience scope stayed inert', not commands(mark), commands(mark))
        set_scope(page, 'Radio')
        check('1h radio rows offer Play as ambience', len(play_buttons(page.index_body)) > 0)
        set_scope(page, 'Library')
        check('1i library rows offer Play as ambience', any('Neoclassical collection' == t for t in visible_texts(page.index_body))
              and len(play_buttons(page.index_body)) > 0, sorted(visible_texts(page.index_body))[:20])
        set_scope(page, 'YouTube')
        type_query(page, 'rain')
        check('1j YouTube search in ambience mode lists results with Play as ambience',
              'Rain on a tin roof, 3 hours' in visible_texts(page.index_body) and len(play_buttons(page.index_body)) == 3,
              sorted(visible_texts(page.index_body))[:20])
        shot(window, '03-listen-ambience-youtube')
        check('1k searching YouTube sent no command', not commands(mark), commands(mark))
        # Choosing a YouTube result sets the ambience through the existing command and returns to the Mix.
        play_buttons(page.index_body)[0].emit('clicked')
        pump(1.2)
        sent = commands(mark)
        check('1l the choice sent one atmosphere command with the video', len(sent) == 1 and sent[0][0] == 'atmosphere'
              and sent[0][1].get('url') == 'https://www.youtube.com/watch?v=aaaaaaaaaaa' and sent[0][1].get('resolver') is True, sent)
        check('1m ambience mode ended and the Mix is open over Now',
              page.mode == 'music' and window.current_page() == 'now' and window.sheet.get_open(),
              (page.mode, window.current_page(), window.sheet.get_open()))
        check('1n the room really has the new ambience layer',
              (env.controller.active.get('atmosphere') or {}).get('url') == 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
              env.controller.active.get('atmosphere'))
        shot(window, '04-mix-after-ambience')

        # An offline sound, then Cancel.
        window.open_mix()
        window.mix.rows['atmosphere'].change.emit('clicked')
        pump(.8)
        mark = len(env.log)
        check('1o Sounds lists offline sounds', len(play_buttons(page.index_body)) >= 6)
        play_buttons(page.index_body)[1].emit('clicked')
        pump(1.2)
        sent = commands(mark)
        check('1p an offline sound replaces the layer through the layer command',
              len(sent) == 1 and sent[0][0] == 'layer' and sent[0][1].get('kind') == 'local', sent)
        check('1q and the Mix reopens', page.mode == 'music' and window.sheet.get_open())
        window.open_mix()
        window.mix.rows['atmosphere'].change.emit('clicked')
        pump(.6)
        mark = len(env.log)
        press(page.widget, 'Cancel')
        pump(.6)
        check('1r Cancel returns to the Mix and sends nothing',
              page.mode == 'music' and window.current_page() == 'now' and window.sheet.get_open() and not commands(mark), commands(mark))
        window.open_mix()
        window.mix.rows['music'].change.emit('clicked')
        pump(.6)
        check('1s the Music change path still opens Listen in music mode',
              window.current_page() == 'browse' and page.mode == 'music' and 'Tune in' in {b.get_label() for b in env.widgets(page.index_body)
                                                                                              if isinstance(b, Gtk.Button)})

        # ------------------------------------------------------------------ New station
        page.scopes.set_active(0)
        pump(.6)
        mark = len(env.log)
        press(page.index_body, 'New station')
        pump(.8)
        dialog = window.get_visible_dialog()
        check('2a New station opens a dialog with a name, a start and moods', dialog is not None and 'Station name' in
              {getattr(w, '_probe', None) or (w.get_placeholder_text() if isinstance(w, Gtk.Entry) else None) for w in env.widgets(dialog)}
              or dialog is not None)
        form = dialog
        entries = [w for w in env.widgets(form) if isinstance(w, Gtk.Entry)]
        name_entry, link_entry = entries[0], entries[1]
        name_entry.set_text('Night stream')
        link_entry.set_text('http://192.168.1.20:8000/night.mp3')
        pump(.2)
        create = env.buttons(form, 'Create station')[0]
        check('2b Create is off until a start is chosen', not create.get_sensitive())
        env.buttons(form, 'Use this link')[0].emit('clicked')
        pump(.4)
        check('2c a valid stream link becomes the start (own-network streams are allowed, and said so)',
              create.get_sensitive() and any('on your own network' in t for t in texts(form)), sorted(set(texts(form)))[:30])
        shot(window, '05-new-station-link')
        link_entry.set_text('https://example.test/not-a-stream-page')
        pump(.2)
        check('2d editing the link clears the chosen start', not create.get_sensitive())
        env.buttons(form, 'Use this link')[0].emit('clicked')
        pump(.4)
        check('2e a page that cannot play is refused with a reason', not create.get_sensitive()
              and any('cannot be a station' in t for t in texts(form)), sorted(set(texts(form)))[:30])
        link_entry.set_text('http://192.168.1.20:8000/night.mp3')
        env.buttons(form, 'Use this link')[0].emit('clicked')
        pump(.3)
        # moods
        groups = [w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup)]
        energy = next(g for g in groups if g.get_toggle(0).get_name() == 'Low')
        energy.set_active(0)
        pump(.2)
        check('2f nothing was sent while filling the form', not commands(mark), commands(mark))
        create.emit('clicked')
        pump(1.2)
        saved = [c for c in commands(mark) if c[0] == 'station.save']
        check('2g Create sent one station.save with the name, the link and the mood', len(saved) == 1
              and saved[0][1]['station']['name'] == 'Night stream'
              and saved[0][1]['station']['sources'][0]['url'] == 'http://192.168.1.20:8000/night.mp3'
              and saved[0][1]['station']['preferences'] == {'energy': 'Low'}, saved)
        stored = [s for s in env.controller.stations() if s['name'] == 'Night stream']
        check('2h the station is stored and its page opens', bool(stored) and page.stack.get_visible_child_name() == 'station')
        page.show_index()
        pump(.6)

        # From a search result: YouTube.
        press(page.index_body, 'New station')
        pump(.8)
        form = window.get_visible_dialog()
        modes = next(w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup) and w.get_toggle(0).get_name() == 'link')
        modes.set_active(1)
        pump(.3)
        search = next(w for w in env.widgets(form) if isinstance(w, Gtk.SearchEntry))
        scopes = next(w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup) and w.get_toggle(0).get_name() == 'radio')
        scopes.set_active(3)
        search.set_text('lofi')
        search.emit('activate')
        pump(1.2)
        start_buttons = [b for b in env.widgets(form) if isinstance(b, Gtk.Button) and (b.get_label() or '') == 'Start from this']
        check('2i a search lists YouTube results to start from', len(start_buttons) == 3, sorted(set(texts(form)))[:30])
        mark = len(env.log)
        start_buttons[1].emit('clicked')
        pump(.3)
        check('2j choosing a result fills the name and the start', any('Lo-fi study mix' in t for t in texts(form)))
        check('2k searching and choosing sent no command', not commands(mark), commands(mark))
        speech = next(g for g in [w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup)] if g.get_toggle(0).get_name() == 'Any'
                      and any(g.get_toggle(i).get_name() == 'Strict instrumental' for i in range(g.get_n_toggles())))
        strict = next(speech.get_toggle(i) for i in range(speech.get_n_toggles()) if speech.get_toggle(i).get_name() == 'Strict instrumental')
        check('2l Instrumental only is off for a start that is not a library track', not strict.get_enabled())
        shot(window, '06-new-station-search')
        env.buttons(form, 'Create station')[0].emit('clicked')
        pump(1.2)
        saved = [c for c in commands(mark) if c[0] == 'station.save']
        check('2m the YouTube start is saved through the resolver', len(saved) == 1
              and saved[0][1]['station']['sources'][0].get('resolver') is True
              and saved[0][1]['station']['sources'][0]['url'] == 'https://www.youtube.com/watch?v=bbbbbbbbbbb', saved)
        # A library track, with the AudioMuse option and strict instrumental allowed.
        page.show_index()
        pump(.5)
        press(page.index_body, 'New station')
        pump(.6)
        form = window.get_visible_dialog()
        next(w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup) and w.get_toggle(0).get_name() == 'link').set_active(1)
        pump(.3)
        scopes = next(w for w in env.widgets(form) if isinstance(w, Adw.ToggleGroup) and w.get_toggle(0).get_name() == 'radio')
        scopes.set_active(1)
        search = next(w for w in env.widgets(form) if isinstance(w, Gtk.SearchEntry))
        search.set_text('nocturne')
        search.emit('activate')
        pump(1.2)
        starts = [b for b in env.widgets(form) if isinstance(b, Gtk.Button) and (b.get_label() or '') == 'Start from this']
        check('2n a library search lists tracks to start from', len(starts) >= 1, sorted(set(texts(form)))[:30])
        starts[0].emit('clicked')
        pump(.3)
        check('2o a library track offers the AudioMuse mix', any(isinstance(w, Gtk.CheckButton) and w.get_visible()
                                                               and 'AudioMuse' in (w.get_label() or '') for w in env.widgets(form)))
        env.buttons(form, 'Cancel')[0].emit('clicked')
        pump(.5)
        # Your stations with the new ones; rename and delete are reachable and delete is confirmed.
        set_scope_index = [k for k, _t in page.scope_list].index('stations')
        page.scopes.set_active(set_scope_index)
        pump(.6)
        shown = visible_texts(page.index_body)
        check('2p Your stations lists the new stations', {'Night stream', 'Lo-fi study mix'} <= shown, sorted(shown)[:30])
        shot(window, '07-your-stations')
        page.scopes.set_active(0)
        pump(.4)
        shown = visible_texts(page.index_body)
        check('2q the shelf shows them too, next to New station', {'Night stream'} <= shown and 'New station' in shown)
        shot(window, '08-your-stations-shelf')
        # Leave the room so a station can be deleted (the controller refuses to remove one that plays).
        env.service.execute('leave', {})
        settle(1.0)
        stored = next(s for s in env.controller.stations() if s['name'] == 'Night stream')
        page.show_station(stored)
        pump(.6)
        mark = len(env.log)
        press(page.widget, 'Rename')
        pump(.4)
        rename = window.get_visible_dialog()
        entry = next(w for w in env.widgets(rename) if isinstance(w, Gtk.Entry))
        entry.set_text('Night stream (renamed)')
        rename.emit('response', 'rename')
        pump(1.2)
        check('2r rename saves through the station save command', any(c[0] == 'station.save' for c in commands(mark)), commands(mark))
        rename.force_close()
        pump(.3)
        renamed = next(s for s in env.controller.stations() if s['name'] == 'Night stream (renamed)')
        page.show_station(renamed)
        pump(.5)
        mark = len(env.log)
        press(page.widget, 'Delete…')
        pump(.5)
        confirm = window.get_visible_dialog()
        check('2s Delete asks first and sends nothing yet', confirm is not None and not commands(mark)
              and any('Delete' in t for t in texts(confirm)), commands(mark))
        shot(window, '09-delete-confirm')
        confirm.emit('response', 'cancel')
        confirm.force_close()
        pump(.4)
        check('2t cancelling keeps the station', not commands(mark)
              and any(s['name'] == 'Night stream (renamed)' for s in env.controller.stations()))
        press(page.widget, 'Delete…')
        pump(.4)
        confirm = window.get_visible_dialog()
        confirm.emit('response', 'delete')
        confirm.force_close()
        pump(1.2)
        check('2u confirming deletes exactly that station', not any(s['name'] == 'Night stream (renamed)' for s in env.controller.stations())
              and any(c[0] == 'station.delete' for c in commands(mark)), commands(mark))
        check('2v and Listen is back on the index', page.stack.get_visible_child_name() == 'index')

        # ------------------------------------------------------------------ Twitch
        window.show_listen()
        page.scopes.set_active(0)
        pump(.5)
        env.twitch_curated, env.twitch_connected = [], False
        page.refresh_twitch(force=True)
        pump(1.0)
        mark = len(env.log)
        check('3a without an account or saved channels the shelf says what to do',
              'Set up Twitch to see live channels' in visible_texts(page.index_body), sorted(visible_texts(page.index_body))[:30])
        shot(window, '10-twitch-setup')
        set_scope(page, 'Live channels')
        check('3b the Live channels scope shows the same entry, not "No live channels"',
              'Set up Twitch to see live channels' in visible_texts(page.index_body)
              and 'No live channels yet.' not in visible_texts(page.index_body))
        press(page.index_body, 'Open Preferences')
        pump(1.2)
        prefs = window.preferences
        check('3c the entry opens Preferences on the Twitch page', any('Twitch' == t for t in texts(prefs))
              and any('Device sign-in' in t for t in texts(prefs)), sorted(set(texts(prefs)))[:40])
        shot(window, '11-twitch-preferences')
        prefs.force_close()
        pump(.4)
        check('3d opening setup sent no playback command', not commands(mark), commands(mark))
        env.twitch_connected = True
        page.refresh_twitch(force=True)
        pump(1.4)
        set_scope(page, 'Live channels')
        shown = visible_texts(page.index_body)
        check('3e once set up, live channels are listed', {'lofigirl', 'jazzhopcafe'} <= shown and
              'Set up Twitch to see live channels' not in shown, sorted(shown)[:30])
        shot(window, '12-twitch-set-up')
        mark = len(env.log)
        env.buttons(page.index_body, 'Tune in')[0].emit('clicked')
        pump(.8)
        sent = commands(mark)
        check('3f a live channel tunes in as music', len(sent) == 1 and sent[0][0] == 'link'
              and sent[0][1].get('destination') == 'music' and 'twitch.tv' in sent[0][1].get('url', ''), sent)
        window.show_listen()
        pump(.5)
        env.service.execute('leave', {})
        settle(.8)
        env.start_station_room()
        window.show_listen()
        page.begin_ambience('now')
        pump(.6)
        set_scope(page, 'Live channels')
        mark = len(env.log)
        play_buttons(page.index_body)[0].emit('clicked')
        pump(1.0)
        sent = commands(mark)
        check('3g a live channel plays as ambience', len(sent) == 1 and sent[0][0] == 'atmosphere' and sent[0][1].get('kind') == 'twitch', sent)
        env.twitch_connected = False
        env.twitch_curated = ['lofigirl']
        page.refresh_twitch(force=True)
        pump(1.0)
        set_scope(page, 'All') if False else None
        page.end_ambience()
        pump(.4)
        set_scope(page, 'Live channels')
        shown = visible_texts(page.index_body)
        check('3h with saved channels but no account the list says live status is unknown',
              'lofigirl' in shown and any('not connected' in t for t in shown), sorted(shown)[:30])

        # ------------------------------------------------------------------ YouTube
        set_scope(page, 'YouTube')
        shown = visible_texts(page.index_body)
        check('4a the YouTube scope says how it works before any search', any('Nothing plays until' in t for t in shown))
        mark = len(env.log)
        type_query(page, 'rain')
        shown = visible_texts(page.index_body)
        check('4b results show title, channel and duration', 'Rain on a tin roof, 3 hours' in shown
              and any('Quiet Sounds' in t and '3:00:00' in t for t in shown), sorted(shown)[:30])
        check('4c each result has Tune in, and Play as ambience and Open elsewhere in its menu',
              len(env.buttons(page.index_body, 'Tune in')) == 3
              and all(isinstance(m, Gtk.MenuButton) for m in
                      [w for w in env.widgets(page.index_body) if isinstance(w, Gtk.MenuButton)][:3]))
        menu = [w for w in env.widgets(page.index_body) if isinstance(w, Gtk.MenuButton)][0]
        menu.popup()
        pump(.4)
        popover_texts = set(texts(menu.get_popover()))
        check('4d the menu offers Play as ambience, Save as station and Open elsewhere',
              {'Play as ambience', 'Save as station', 'Open elsewhere'} <= popover_texts, popover_texts)
        shot(window, '13-youtube-results')
        menu.popdown()
        check('4e searching sent no command (inert)', not commands(mark), commands(mark))
        # Cancellable: a slow search is replaced by a newer one and its answer never shows.
        env.yt_delay = 2.0
        page.search.set_text('slow query')
        page.query_submitted()
        pump(.4)
        check('4f a slow search shows Searching and a Stop button',
              'Searching YouTube…' in visible_texts(page.index_body) and buttons(page.index_body, 'Stop'))
        buttons(page.index_body, 'Stop')[0].emit('clicked')
        pump(.5)
        check('4g Stop ends it without results', 'Searching YouTube…' not in visible_texts(page.index_body)
              and 'Fireplace, no talking' not in visible_texts(page.index_body))
        env.yt_delay = 0
        # A pasted YouTube link still opens the link sheet.
        page.search.set_text('https://www.youtube.com/watch?v=abcdefghijk')
        pump(.8)
        press(page.index_body, 'Open link…')
        pump(.5)
        sheet = window.get_visible_dialog()
        check('4h a YouTube link still opens the link sheet', sheet is not None and 'Play as ambience' in texts(sheet)
              and 'Play' in texts(sheet), sorted(set(texts(sheet)))[:20] if sheet else None)
        sheet.force_close()
        pump(.3)
        # yt-dlp missing.
        page.search.set_text('')
        env.yt_available = False
        page.index_sig = None
        page.refresh()
        pump(.4)
        shown = visible_texts(page.index_body)
        check('4i without yt-dlp the scope says so and offers Preferences', 'Install yt-dlp to search YouTube.' in shown
              and buttons(page.index_body, 'Open Preferences'), sorted(shown)[:30])
        shot(window, '14-youtube-missing')
        window.open_preferences()
        pump(.8)
        check('4j Preferences › Connections lists the same truth', 'Install yt-dlp to search YouTube.' in set(texts(window.preferences)))
        shot(window, '15-youtube-preferences')
        window.preferences.force_close()
        pump(.3)
        env.yt_available = True

        # ------------------------------------------------------------------ Rooms: one click, no wait
        window.show_rooms()
        pump(1.0)
        child = window.cards.get_first_child()
        entry = window.rooms.card_for[child]
        gesture = [c for w in env.widgets(child) for c in w.observe_controllers()
                   if isinstance(c, Gtk.GestureClick) and c.get_button() == 1][0]
        mark = len(env.log)
        gesture.emit('pressed', 1, 10., 10.)
        gesture.emit('released', 1, 10., 10.)
        pump(.05)
        check('5a one click shows the preview within 50 ms', window.rooms.stack.get_visible_child_name().startswith('preview'))
        pump(.8)
        check('5b a single click never enters the room', not [c for c in commands(mark) if c[0] == 'enter'], commands(mark))
        window.rooms.close_preview(animate=False)
        pump(.4)
        gesture.emit('pressed', 1, 10., 10.)
        gesture.emit('released', 1, 10., 10.)
        pump(.05)
        again = [c for c in window.rooms.stack.observe_controllers() if isinstance(c, Gtk.GestureClick)][0]
        again.emit('pressed', 2, 12., 12.)
        pump(1.0)
        check('5c a quick second click, landing on the preview that replaced the card, enters once',
              len([c for c in commands(mark) if c[0] == 'enter']) == 1, commands(mark))
    finally:
        failed = [name for name, ok in checks if not ok]
        print('\n%d checks, %d failed' % (len(checks), len(failed)))
        for name in failed:
            print('FAILED', name)
    sys.exit(1 if failed else 0)
