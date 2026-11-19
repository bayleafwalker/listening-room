"""O9-UX evidence: keyboard focus, plain-words errors, the Listen-first path, placement, and widths.

Real GTK, the real session controller on the session test double and fixture providers; nothing
reaches the desktop session, the network or real credentials. Prints PASS/FAIL lines and writes the
screenshots named in the O9-UX brief.

  scripts/listening-dev.sh probe OUT listening-o9-ux
"""
import json
from unittest import mock

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o9-ux')
checks = []


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:300]))


with env:
    Gtk, Gdk = env.Gtk, env.Gdk
    pump, settle, buttons, shot = env.pump, env.settle, env.buttons, env.shot
    import session_client
    import atmosphere_catalogue

    def label_texts(root):
        return {w.get_text() for w in env.widgets(root) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()}

    def key(window, keyval):
        controllers = [c for c in window.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
        controllers.sort(key=lambda c: c.get_propagation_phase() != Gtk.PropagationPhase.CAPTURE)
        return any(c.emit('key-pressed', keyval, 0, 0) for c in controllers)

    def bounds(widget, window):
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok and widget.get_mapped() else None

    def overlaps(a, b):
        return bool(a and b and a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])

    def toast_widgets(window):
        return [w for w in env.widgets(window.toasts) if type(w).__name__ == 'ToastWidget' or w.get_css_name() == 'toast']

    def leave():
        env.service.execute('leave', {})
        settle(1.0)

    numbers = {}
    # ------------------------------------------------------------------ 6: the Listen-first path (no room)
    window = env.launch(page='listen')
    keys = [t for _k, t in window.listen.scope_list]
    window.listen.scopes.set_active(keys.index('Radio'))
    pump(.6)
    tune = buttons(window.listen.index_body, 'Tune in')[0]
    tune.emit('clicked')
    settle(2.0)
    pump(1.2)
    active = env.controller.active or {}
    title = window.room_name.get_text()
    check('6a Tune in with no room: Now has a real title, not "Your listening room"',
          window.current_page() == 'now' and title and title != 'Your listening room', (title, active.get('name')))
    check('6b ... it offers Choose a scene', window.choose_scene.get_mapped(), '')
    check('6c ... and the scene is not black (fallback still art)', bool(window.scene.key and window.scene.key[0]), window.scene.key)
    check('2a after Tune in play has focus', window.get_focus() is window.play, repr(window.get_focus()))
    shot(window, 'o9-06a-tune-in-without-room')
    check('6d Back returns to Listen, where Tune in was pressed', window.back.get_tooltip_text() == 'Back to Listen', window.back.get_tooltip_text())
    key(window, Gdk.KEY_Escape)
    pump(.6)
    check('6e Esc in Now goes back to Listen', window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'listen')
    window.show_now()
    pump(.4)
    window.show_listen()
    pump(.3)
    key(window, Gdk.KEY_Escape)
    pump(.6)
    check('6f Esc in Listen goes back to Now when Listen was opened from Now', window.current_page() == 'now')
    leave()
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 2: focus and Space
    window = env.launch(page=None)
    pump(.6)
    check('2b at launch with a last room nothing plays and focus is on the hero button or nothing',
          window.get_focus() in (None, window.hero_button) and not env.controller.active, repr(window.get_focus()))
    shot(window, 'o9-02a-launch-hero-focus')
    cards = list(window.rooms.card_for.values())
    window.rooms.open_preview(cards[0])
    pump(.5)
    window.rooms.press_enter()
    settle(2.0)
    pump(1.0)
    check('2c after Enter room Now shows and play has focus', window.current_page() == 'now' and window.get_focus() is window.play,
          repr(window.get_focus()))
    shot(window, 'o9-02b-now-after-enter-focus-play')
    mark = len(env.log)
    window.back.grab_focus()
    key(window, Gdk.KEY_space)
    settle(1.0)
    check('2d Space pauses even with Back focused, and stays in Now', [a for a, _ in env.commands(mark)] == ['quiet']
          and window.current_page() == 'now', env.commands(mark))
    key(window, Gdk.KEY_space)
    settle(1.0)
    leave()
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 6: Tune in that changes a room's music
    window = env.launch(page='rooms')
    keys = [t for _k, t in window.listen.scope_list]
    cards = list(window.rooms.card_for.values())
    window.rooms.enter(cards[0])
    settle(2.5)
    pump(.8)
    window.show_listen()
    window.listen.scopes.set_active(keys.index('Radio'))
    pump(.6)
    buttons(window.listen.index_body, 'Tune in')[0].emit('clicked')
    settle(2.5)
    pump(1.5)
    toasts = label_texts(window.toasts)
    check('6g Tune in over a room marks it Changed', window.changed_chip.get_mapped(), '')
    check('6h ... with Save as my room', any(t.startswith('Changed: ') for t in toasts)
          and bool(buttons(window.toasts, 'Save as my room')), sorted(toasts)[:10])
    shot(window, 'o9-06b-changed-save-as-my-room')
    pump(5.5)

    # ------------------------------------------------------------------ 3: plain-words errors
    env.atmosphere_error = '[Errno 2] No such file or directory: /fixture/missing.ogg'
    sound = atmosphere_catalogue.SOUNDS[0]
    env.service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': sound['id']})
    settle(1.5)
    window.show_now()
    pump(1.0)
    banner = label_texts(window.banner)
    check('3a the layer banner headline is a plain sentence, no errno', window.banner.get_mapped()
          and not any('Errno' in t for t in banner), sorted(banner))
    check('3b the raw cause is behind Details', window.banner_details.get_visible() and not window.banner_details.get_expanded()
          and 'Errno' in window.banner_details.text.get_text())
    window.banner_details.set_expanded(True)
    pump(.4)
    shot(window, 'o9-03a-layer-failure-details')
    window.banner_details.set_expanded(False)
    env.atmosphere_error = None
    with mock.patch.object(session_client, 'request', side_effect=ConnectionRefusedError(111, 'Connection refused')), \
            mock.patch.object(session_client, 'ensure_service', side_effect=lambda *a, **k: None):
        window.poll()
        pump(1.2)
        check('3c an unreachable room service shows a persistent banner with Start again',
              window.offline_bar.get_reveal_child() and window.offline_start.get_mapped())
        check('3d ... and Now does not look live', not window.play.get_sensitive() and window.chip.get_text() == 'Not connected')
        shot(window, 'o9-03b-room-service-not-running')
    pump(1.5)
    check('3e the banner goes when the service answers again', not window.offline_bar.get_reveal_child())
    leave()
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 7: placement
    snapshot = env.service.execute('inspect', {})
    check('7a the default placement is Here (the current workspace)', snapshot['settings'].get('placement') == 'here',
          snapshot['settings'])
    env.start_station_room()
    window = env.launch(page=None)
    window.open_preferences()
    pump(.8)
    prefs = window.preferences
    prefs.set_visible_page(prefs.pages['scene'])
    pump(.6)
    check('7b Preferences shows Here as the default placement', prefs.placement_row.get_selected() == 1, prefs.placement_row.get_selected())
    shot(window, 'o9-07a-prefs-placement-here')
    prefs.close()
    pump(.3)
    window.open_mix()
    pump(.8)
    shot(window, 'o9-07b-mix-scene-placement')
    window.sheet.set_open(False)
    pump(.3)
    window.close()
    pump(.3)

    # ------------------------------------------------------------------ 8: widths
    for width in (360, 640, 900):
        window = env.launch(width=width, height=720, page=None)
        window.show_now()
        pump(1.0)
        page = window.current_page()
        if page == 'now':
            icon = window.volume.get_parent().get_first_child()
            play, vol = bounds(window.play, window), bounds(icon, window)
            mix = bounds(window.mix_button, window)
            numbers['width-%d' % width] = {'play': play, 'volume_icon': vol, 'mix': mix}
            check('8a at %d px the play button and the volume icon do not overlap' % width, not overlaps(play, vol), (play, vol))
            check('8b at %d px the Mix button and the play button do not overlap' % width, not overlaps(play, mix), (play, mix))
        else:
            check('8c at %d px the mini layout stands in' % width, page == 'mini', page)
        window.toast('A toast that must not cover the play button')
        pump(.8)
        toast = [bounds(w, window) for w in toast_widgets(window)]
        target = bounds(window.mini_player.play if page == 'mini' else window.play, window)
        numbers['toast-%d' % width] = {'toast': toast, 'play': target}
        check('8d at %d px the toast does not cover the play button' % width, toast and not any(overlaps(t, target) for t in toast),
              (toast, target))
        shot(window, 'o9-08-now-%dpx' % width)
        if page == 'now':
            window.open_mix()
            pump(1.0)
            window.toast('A toast over the Mix')
            pump(.8)
            last = bounds(window.mix.rows['picture'], window)
            toast = [bounds(w, window) for w in toast_widgets(window)]
            check('8e at %d px no toast covers the Mix\'s last row' % width, not any(overlaps(t, last) for t in toast), (toast, last))
            shot(window, 'o9-08-mix-%dpx' % width)
            window.sheet.set_open(False)
        window.close()
        pump(1.5)
    leave()
    window = env.launch(width=360, height=600, page=None)
    pump(.8)
    check('8f the idle mini layout offers Browse rooms as well as Return',
          bool(buttons(window.mini_player, 'Browse rooms')) and any('Return' in (b.get_label() or '') for b in env.widgets(window.mini_player)
                                                                     if isinstance(b, Gtk.Button) and b.get_mapped()))
    shot(window, 'o9-08-idle-mini-360px')
    env.press(window.mini_player, 'Browse rooms')
    pump(.8)
    check('8g Browse rooms at 360 px shows Rooms', window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms',
          window.current_page())
    shot(window, 'o9-08-rooms-360px')
    window.close()
    pump(.3)

    (env.output / 'o9-numbers.json').write_text(json.dumps(numbers, indent=1))
    failed = [name for name, ok in checks if not ok]
    print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)))
