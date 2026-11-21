"""O10-LISTEN evidence: Listen's mode header, Twitch categories and paging, and layout from 360 px up.

Real GTK on private Xvfb, the session controller on its test double and fixture providers (see o6_env):
nothing reaches the network, the desktop session or real credentials. Prints PASS/FAIL lines and writes
screenshots.

  scripts/listening-dev.sh probe OUT listening-o10l-ux
"""
import copy
import json
from unittest import mock

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o10l-ux')
checks = []


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]))


def stream(login, game, viewers, title='', language='en'):
    return {'login': login, 'name': login.title(), 'url': 'https://www.twitch.tv/' + login, 'live': True,
            'title': title or 'Stream of ' + login, 'curated': False, 'category': game, 'viewers': viewers,
            'language': language.upper(), 'thumbnail': ''}


with env:
    Gtk, Gdk, GLib = env.Gtk, env.Gdk, env.GLib
    pump, settle, buttons, shot = env.pump, env.settle, env.buttons, env.shot
    twitch = env.twitch_channels

    def bounds(widget, window):
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok and widget.get_mapped() else None

    def overlaps(a, b):
        return bool(a and b and a[0] < b[0] + b[2] - .5 and b[0] < a[0] + a[2] - .5 and a[1] < b[1] + b[3] - .5 and b[1] < a[1] + a[3] - .5)

    def inside(box, window):
        return bool(box and box[0] >= -.5 and box[1] >= -.5 and box[0] + box[2] <= window.get_width() + .5
                    and box[1] + box[3] <= window.get_height() + .5)

    def label_texts(root):
        return [w.get_text() for w in env.widgets(root) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()]

    def toast_widgets(window):
        return [w for w in env.widgets(window.toasts) if w.get_css_name() == 'toast']

    def leave():
        env.service.execute('leave', {})
        settle(1.0)

    # ------------------------------------------------------------------ 1: the mode header
    env.start_station_room()
    window = env.launch(page=None)
    pump(1.0)
    window.show_now()
    pump(.6)
    listen = window.listen
    window.choose_other('atmosphere')
    pump(.8)
    check('1b Ambience › Change… shows Listen', window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'listen')
    title = window.mode_title.get_label()
    check('1c the header names the mode and the room: "Choose ambience · …"', title.startswith('Choose ambience') and (window.snapshot['active'] or {}).get('name', '?') in title
          and window.mode_title.get_mapped(), title)
    check('1d the Rooms | Listen switcher is gone while the mode lasts', not window.switcher.get_mapped())
    check('1e a Back control is in the header and is named', window.mode_back.get_mapped() and window.mode_back.get_tooltip_text() == 'Back to Mix',
          window.mode_back.get_tooltip_text())
    check('1f the page still says what to press', any(t.startswith('Choose an ambience') for t in label_texts(listen.widget)))
    shot(window, 'o10l-01-ambience-header')
    window.mode_back.emit('clicked')
    pump(.8)
    check('1g Back ends the mode and returns to the room', listen.mode == 'music' and window.current_page() == 'now', (listen.mode, window.current_page()))
    window.show_listen()
    pump(.6)
    check('1h the switcher is back and the mode header is gone', window.switcher.get_mapped() and not window.mode_back.get_mapped()
          and not window.mode_title.get_mapped())
    for route, leave_listen in (('Rooms', window.show_rooms), ('Now', window.show_now)):
        window.choose_other('atmosphere')
        pump(.6)
        leave_listen()
        pump(.6)
        check('1i leaving by %s ends the mode and restores the header' % route,
              listen.mode == 'music' and not window.mode_back.get_mapped() and window.header.get_title_widget() is window.switcher,
              (listen.mode, window.header.get_title_widget()))
    window.choose_other('atmosphere')
    pump(.6)
    mark = len(env.log)
    sounds = buttons(listen.index_body, 'Play as ambience')
    if sounds:
        sounds[0].emit('clicked')
        settle(1.2)
    check('1j choosing an ambience ends the mode and the header returns',
          listen.mode == 'music' and not window.mode_back.get_mapped(), (listen.mode, bool(sounds)))
    leave()
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 2: Twitch
    env.twitch_connected = True
    music = [stream('lofigirl', 'Music', 1234, 'beats to relax'), stream('jazzhopcafe', 'Music', 900)] + [
        stream('music%02d' % n, 'Music', 800 - n) for n in range(70)]
    asmr = [stream('whisperer', 'ASMR', 321, 'Rain on a mic', 'fi'), stream('tapper', 'ASMR', 40)]
    music_more = [stream('late%02d' % n, 'Music', 20 - n) for n in range(15)]
    calls = []

    def discover(style, curated=None, client_id=None, access_token=None, after=None, game_id=None, category=None):
        calls.append((style, after, category))
        rows = {'music': (music, 'cursor-1', '42'), 'asmr': (asmr, None, '43')}[style]
        if after:
            return {'status': 'live', 'channels': copy.deepcopy(music_more), 'cursor': None, 'game_id': game_id, 'curated_status': {}, 'message': ''}
        return {'status': 'live', 'channels': copy.deepcopy(rows[0]), 'cursor': rows[1], 'game_id': rows[2], 'curated_status': {}, 'message': ''}

    with mock.patch.object(twitch, 'discover', side_effect=discover):
        window = env.launch(page='listen')
        listen = window.listen
        listen.refresh_twitch(force=True)
        pump(1.2)
        keys = [t for _k, t in listen.scope_list]
        listen.scopes.set_active(keys.index('Live channels'))
        pump(.8)
        names = [listen.channel_chips.get_toggle(i).get_label() for i in range(listen.channel_chips.get_n_toggles())]
        check('2a category chips: All, Music, ASMR and Your channels', {'All', 'Music', 'ASMR', 'Your channels'} <= set(names), names)
        shot(window, 'o10l-02a-twitch-all')
        texts = label_texts(listen.index_body)
        check('2b All shows ASMR channels next to Music (the ASMR loss is fixed)', 'Whisperer' in texts and 'Lofigirl' in texts, texts[:12])
        check('2c a row says category, viewers and language', any(t == 'ASMR · 321 watching · FI' for t in texts)
              and any(t == 'Music · 1.2 k watching · EN' for t in texts), [t for t in texts if 'watching' in t][:6])
        check('2d0 rows have a picture slot (an avatar), not a monogram', any(isinstance(w, env.Adw.Avatar) for w in env.widgets(listen.index_body)))
        check('2d a row shows the stream title', 'Rain on a mic' in texts and 'beats to relax' in texts)

        def pick(name):
            index = names.index(name)
            listen.channel_chips.set_active(index)
            pump(.6)

        pick('ASMR')
        texts = label_texts(listen.index_body)
        check('2e the ASMR chip lists only ASMR streams', 'Whisperer' in texts and 'Tapper' in texts and 'Lofigirl' not in texts, texts[:10])
        shot(window, 'o10l-02b-twitch-asmr')
        pick('Music')
        rows = [w for w in env.widgets(listen.index_body) if isinstance(w, Gtk.Box) and 'listen-row' in w.get_css_classes()]
        check('2f Music lists every stream read, past the old 60 cap', len(rows) == 72, len(rows))
        more = buttons(listen.index_body, 'Show more')
        check('2g Music has a Show more while Twitch has another page', len(more) == 1, len(more))
        mark = len(calls)
        if more:
            more[0].emit('clicked')
            pump(1.2)
        rows = [w for w in env.widgets(listen.index_body) if isinstance(w, Gtk.Box) and 'listen-row' in w.get_css_classes()]
        check('2h Show more reads the next page with the cursor and appends it', ('music', 'cursor-1', None) in calls[mark:]
              and len(rows) == 87, (calls[mark:], len(rows)))
        check('2i on the last page Show more goes away', not buttons(listen.index_body, 'Show more'))
        shot(window, 'o10l-02c-twitch-music-more')
        pick('All')
        drop = listen.sort_drop
        drop.set_selected(1)
        pump(.6)
        first = [t for t in label_texts(listen.index_body) if 'watching' in t][0]
        check('2j Fewest watched puts the smallest stream first', ' · 5 watching ·' in first or ' · 6 watching ·' in first, first)
        drop.set_selected(0)
        pump(.4)
        mark = len(env.log)
        listen.scopes.set_active(keys.index('Live channels'))
        tune = buttons(listen.index_body, 'Tune in')
        check('2k browsing sent nothing to the room', not [c for c in env.commands(mark) if not c[0].startswith('metadata.')], env.commands(mark))
        window.close()
        pump(.3)
    env.twitch_connected = False

    # ------------------------------------------------------------------ 3: layout from 360 px up
    leave()
    env.start_station_room()
    for width in (360, 560, 1000):
        window = env.launch(width=width, height=720, page=None)
        window.show_now()
        pump(1.0)
        page = window.current_page()
        if page == 'mini':
            player = window.mini_player
            expand = bounds(player.expand_button, window)
            others = {'name': player.name, 'play': player.play, 'volume': player.volume_box, 'timer': player.timer_button}
            for key, widget in others.items():
                check('3a at %d px the expand button does not overlap the %s' % (width, key), not overlaps(expand, bounds(widget, window)),
                      (expand, bounds(widget, window)))
            controls = [w for w in env.widgets(window.header) if isinstance(w, Gtk.WindowControls) and w.get_mapped()]
            check('3b at %d px the expand button is clear of the title bar window controls' % width,
                  expand is not None and not any(overlaps(expand, bounds(c, window)) for c in controls), (expand, [bounds(c, window) for c in controls]))
            check('3c at %d px the expand button is inside the window' % width, inside(expand, window), expand)
            check('3d at %d px the room name stays clear of the window controls' % width,
                  not any(overlaps(bounds(player.name, window), bounds(c, window)) for c in controls))
        name_widget = window.mini_player.name if page == 'mini' else window.room_name
        line_widget = window.mini_player.line if page == 'mini' else window.now_line
        window.toast('Ambience: Rain on glass', 'Undo', lambda: None)
        pump(1.0)
        toasts = [bounds(w, window) for w in toast_widgets(window)]
        name_box, line_box = bounds(name_widget, window), bounds(line_widget, window)
        check('3e at %d px a toast covers neither the room name nor the now-playing line' % width,
              toasts and not any(overlaps(t, name_box) or overlaps(t, line_box) for t in toasts), (toasts, name_box, line_box))
        target = bounds(window.mini_player.play if page == 'mini' else window.play, window)
        check('3f at %d px ... nor the play button' % width, not any(overlaps(t, target) for t in toasts), (toasts, target))
        shot(window, 'o10l-03-toast-%dpx' % width)
        if page == 'now':
            check('3g while a toast with an action shows, the controls do not fade', not window.fades(), window.action_toasts)
        window.close()
        pump(1.2)
    leave()
    env.start_station_room()
    window = env.launch(width=360, height=640, page=None)
    pump(.8)
    window.browse_rooms()
    pump(1.0)
    rooms = window.rooms
    check('3h Browse rooms at 360 px shows Rooms', window.current_page() == 'browse')
    check('3i the page asks for no more than the window is wide', rooms.widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 360
          and window.browse.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 360,
          (rooms.widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0], window.browse.measure(Gtk.Orientation.HORIZONTAL, -1)[0]))
    check('3j the view switcher is a bottom bar, with Listen reachable', window.switcher_bar.get_reveal() and not window.switcher.get_mapped())
    chips = [bounds(c, window) for c in rooms.chips.values()]
    check('3k every mood chip is inside the window', all(inside(c, window) for c in chips), chips)
    cards = [bounds(c, window) for c in env.widgets(rooms.cards) if isinstance(c, Gtk.FlowBoxChild)]
    check('3l the room cards fit the width', all(c is None or inside(c, window) or c[0] + c[2] <= 360.5 for c in cards), cards[:3])
    shot(window, 'o10l-03-rooms-360px')
    window.browse.set_visible_child_name('listen')
    pump(1.0)
    listen = window.listen
    check('3p with a room playing, Listen at 360 px asks for no more than the window (bar included)',
          window.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 360, window.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
    check('3q ... and the search entry ends inside the window', (bounds(listen.search, window) or (0, 0, 0))[0] + (bounds(listen.search, window) or (0, 0, 0))[2] <= 360)
    listen.begin_ambience('now')
    pump(.8)
    check('3r ambience mode at 360 px fits: toolbar, Cancel and search all inside', window.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 360
          and all(inside(bounds(b, window), window) for b in buttons(listen.widget, 'Cancel') + [listen.search]),
          window.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
    listen.end_ambience()
    pump(.4)
    scopes = [bounds(t, window) for t in env.widgets(listen.scopes) if isinstance(t, Gtk.ToggleButton) and t.get_mapped()]
    check('3m Listen at 360 px: the scope chips keep their full words (they scroll, not ellipsize)',
          all(not any(isinstance(x, Gtk.Label) and x.get_layout().is_ellipsized() for x in env.widgets(t))
              for t in env.widgets(listen.scopes) if isinstance(t, Gtk.ToggleButton)))
    check('3n Listen asks for no more than the window is wide', window.browse.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 360,
          window.browse.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
    shot(window, 'o10l-03-listen-360px')
    window.close()
    pump(.3)
    window = env.launch(width=600, height=720, page='listen')
    pump(.8)
    check('3o Listen at 600 px: station cards keep their names (no ellipsis)',
          not any(isinstance(x, Gtk.Label) and x.get_mapped() and x.get_layout().is_ellipsized() and 'row-name' in x.get_css_classes()
                  for x in env.widgets(window.listen.index_body)))
    shot(window, 'o10l-03-listen-600px')
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 4: words
    window = env.launch(page='listen')
    listen = window.listen
    keys = [t for _k, t in listen.scope_list]
    listen.scopes.set_active(keys.index('Library'))
    pump(1.2)
    check('4a Library rows say Play (on demand), not Tune in', bool(buttons(listen.index_body, 'Play')) and not buttons(listen.index_body, 'Tune in'),
          sorted(set(label_texts(listen.index_body)))[:12])
    listen.scopes.set_active(keys.index('Radio'))
    pump(.8)
    check('4b Radio rows keep Tune in (live)', bool(buttons(listen.index_body, 'Tune in')))
    window.close()
    pump(.3)

failed = [name for name, ok in checks if not ok]
print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)))
(env.output / 'o10l-checks.json').write_text(json.dumps(checks, indent=1))
