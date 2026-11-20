"""O9-UX acceptance: independent oracles for keyboard, toasts, plain errors, fresh data, Listen-first,
Hyprland placement, widths and the O6 follow-ups.

Written by the acceptance reviewer, not the implementer. Real GTK on a private Xvfb with its own
D-Bus (tests/probes/o6_env.py re-executes this file there), the real session controller on the
session test double and fixture providers. Nothing reaches the desktop session, the network,
systemd or real credentials. When xdotool is on PATH, keys are real X key events (XTEST), so they
travel GTK's whole capture/target/bubble path including a focused widget's own key handling;
without it they are emitted on the window's key controllers and the log says so.

  scripts/listening-dev.sh shell, then: python3 tests/probes/listening-o9-ux-acceptance.py --output OUT
  (O9_ACCEPTANCE_SECTIONS=K,H limits the run to those sections, for mutant runs)

Every section is isolated: one that raises records a FAIL and the rest still run, so the probe can
also be pointed at older code (a negative control) with --source.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback
from unittest import mock

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o9-ux-acceptance')
checks = []
XDOTOOL = shutil.which('xdotool')
ONLY = set(filter(None, os.environ.get('O9_ACCEPTANCE_SECTIONS', '').split(',')))   # e.g. K,H for a mutant run


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]), flush=True)


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s: section ran without raising' % name, False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1500:]))
        finally:
            for window in list(env.app.get_windows()):
                window.close()
            env.pump(.4)
            try:
                env.start_error = None
                env.start_delay = 0.
                env.atmosphere_error = None
                if env.controller.active:
                    env.service.execute('leave', {})
                    env.settle(1.0)
            except Exception:
                pass
        return function
    return wrap


with env:
    Gtk, Gdk, GLib = env.Gtk, env.Gdk, env.GLib
    pump, settle = env.pump, env.settle
    import listening_hub as hub
    import next_model
    import room_session
    import session_client
    import next_window as next_window_module
    from PIL import Image, ImageStat
    print('keys: ' + ('real X events through xdotool' if XDOTOOL else 'emitted on key controllers (no xdotool on PATH)'))

    TOGGLES = ('quiet', 'resume', 'enter', 'return')
    STARTS = ('music', 'online', 'link', 'station.start', 'station.replay', 'enter', 'return')

    def widgets(root):
        return list(env.widgets(root))

    def texts(root):
        return set(env.texts(root))

    def mapped_texts(root):
        found = set()
        for w in widgets(root):
            if not w.get_mapped():
                continue
            for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle'):
                value = getattr(w, getter, lambda: None)()
                if isinstance(value, str) and value:
                    found.add(value)
        return found

    def toast_texts(window):
        return mapped_texts(window.toasts) - mapped_texts(window.toolbar)

    def toast_widgets(window):
        return [w for w in widgets(window.toasts) if w.get_css_name() == 'toast' and w.get_mapped()]

    def bounds(widget, window):
        if widget is None or not widget.get_mapped():
            return None
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None

    def overlaps(a, b):
        return bool(a and b and a[0] < b[0] + b[2] - .5 and b[0] < a[0] + a[2] - .5
                    and a[1] < b[1] + b[3] - .5 and b[1] < a[1] + a[3] - .5)

    def inside(widget, ancestor):
        node = widget
        while node is not None:
            if node is ancestor:
                return True
            node = node.get_parent()
        return False

    def toggles(mark):
        return [a for a, _ in env.commands(mark) if a in TOGGLES]

    def starts(mark):
        return [a for a, _ in env.commands(mark) if a in STARTS]

    def x_focus(window):
        ids = subprocess.run([XDOTOOL, 'search', '--onlyvisible', '--pid', str(os.getpid())],
                             capture_output=True, text=True, timeout=10).stdout.split()
        surface = window.get_surface()
        xid = None
        try:
            import gi
            gi.require_version('GdkX11', '4.0')
            from gi.repository import GdkX11
            xid = str(GdkX11.X11Surface.get_xid(surface))
        except Exception:
            xid = ids[-1] if ids else None
        if xid:
            subprocess.run([XDOTOOL, 'windowfocus', '--sync', xid], capture_output=True, timeout=10)
        pump(.2)

    def key(window, name):
        """One key press, as real as the environment allows."""
        if XDOTOOL:
            x_focus(window)
            subprocess.run([XDOTOOL, 'key', '--clearmodifiers', name], capture_output=True, timeout=10)
            pump(.4)
            return
        keyval = {'space': Gdk.KEY_space, 'Up': Gdk.KEY_Up, 'Down': Gdk.KEY_Down, 'Escape': Gdk.KEY_Escape}[name]
        controllers = [c for c in window.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
        controllers.sort(key=lambda c: c.get_propagation_phase() != Gtk.PropagationPhase.CAPTURE)
        for controller in controllers:
            if controller.emit('key-pressed', keyval, 0, 0):
                break
        pump(.4)

    def wait(predicate, seconds=6.):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            pump(.2)
        return bool(predicate())

    def radio_tune_buttons(window):
        keys = [t for _k, t in window.listen.scope_list]
        window.listen.scopes.set_active(keys.index('Radio'))
        pump(.6)
        return env.buttons(window.listen.index_body, 'Tune in')

    def enter_first_room(window):
        window.show_rooms()
        pump(.4)
        entries = list(window.rooms.card_for.values())
        entry = next((e for e in entries if e.get('source_id')), entries[0])
        window.rooms.open_preview(entry)
        pump(.6)
        window.rooms.preview_widgets['enter'].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None and env.controller.active is not None, 8)
        pump(.8)
        return entry

    def brightness(path, box=None):
        image = Image.open(path).convert('L')
        if box:
            image = image.crop(box)
        return ImageStat.Stat(image).mean[0]

    # =========================================================================== K: keyboard (criterion 2)
    @section('K')
    def keyboard():
        env.start_station_room()
        window = env.launch(page=None)
        pump(.6)
        check('K1 at launch with a room playing, Now shows and play has the focus',
              window.current_page() == 'now' and window.get_focus() is window.play, (window.current_page(), window.get_focus()))
        mark = len(env.log)
        key(window, 'space')
        settle(.8)
        check('K2 at launch Space pauses (exactly one quiet) and Now stays', toggles(mark) == ['quiet'] and window.current_page() == 'now',
              (env.commands(mark), window.current_page()))
        mark = len(env.log)
        key(window, 'space')
        settle(1.2)
        check('K3 Space again plays (one resume/enter, no quiet)', len(toggles(mark)) == 1 and toggles(mark)[0] in ('resume', 'enter'),
              env.commands(mark))
        wait(lambda: next_model.room_state(env.service.execute('inspect', {})) == 'playing', 4)
        before = window.volume.get_value()
        key(window, 'Down')
        key(window, 'Down')
        down = window.volume.get_value()
        key(window, 'Up')
        up = window.volume.get_value()
        check('K4 Down lowers and Up raises the room volume in Now', down < before and up > down, (before, down, up))
        # A toast never takes the focus, and Space still pauses while one shows.
        window.play.grab_focus()
        window.toast('An acceptance toast with a button', 'Undo', lambda: None)
        window.toast('An urgent acceptance toast', urgent=True)
        pump(1.0)
        focus = window.get_focus()
        check('K5 toasts (with a button, and urgent) never take the focus', focus is window.play
              and not any(inside(focus, t) for t in toast_widgets(window)), focus)
        window.back.grab_focus()
        mark = len(env.log)
        key(window, 'space')
        settle(.8)
        check('K6 Space with Back focused pauses and does not press Back', toggles(mark) == ['quiet']
              and window.current_page() == 'now', (env.commands(mark), window.current_page()))
        # A focused slider keeps its own keys.
        window.volume.grab_focus()
        mark = len(env.log)
        key(window, 'space')
        settle(.6)
        check('K7 Space on the focused volume slider is not play/pause', toggles(mark) == [], env.commands(mark))
        # A dialog keeps its keys.
        window.open_preferences()
        pump(1.0)
        mark = len(env.log)
        vol = window.volume.get_value()
        key(window, 'space')
        key(window, 'Down')
        settle(.6)
        check('K8 with Preferences open, Space and Down do nothing to the room', toggles(mark) == [] and window.volume.get_value() == vol,
              (env.commands(mark), vol, window.volume.get_value()))
        window.preferences.close()
        pump(.6)
        window.save_dialog()
        pump(.8)
        dialog = window.get_visible_dialog()
        window.save_entry.grab_focus()   # Space then types into the name; on a focused Save button it would save
        mark = len(env.log)
        key(window, 'space')
        settle(.6)
        check('K9 with the Save as my room dialog open, Space is not play/pause', dialog is not None and toggles(mark) == [],
              (dialog, env.commands(mark)))
        if dialog is not None:
            dialog.force_close()
            pump(.4)
        # A text entry keeps Space and the arrows.
        window.show_listen()
        pump(.8)
        window.listen.search.set_text('')
        window.listen.search.grab_focus()
        pump(.3)
        vol = window.volume.get_value()
        mark = len(env.log)
        key(window, 'space')
        key(window, 'Up')
        settle(.6)
        check('K10 with the Listen search focused, Space types into it and nothing toggles',
              ' ' in window.listen.search.get_text() and toggles(mark) == [], (repr(window.listen.search.get_text()), env.commands(mark)))
        check('K11 with the Listen search focused, Up does not change the room volume', window.volume.get_value() == vol,
              (vol, window.volume.get_value()))
        window.listen.search.set_text('')
        window.close()
        pump(.4)
        env.service.execute('leave', {})
        settle(1.0)
        # After entering a room.
        window = env.launch(page='rooms')
        enter_first_room(window)
        check('K12 after Enter room, Now shows and play has the focus', window.current_page() == 'now' and window.get_focus() is window.play,
              (window.current_page(), window.get_focus()))
        mark = len(env.log)
        key(window, 'space')
        settle(.8)
        check('K13 after Enter room Space pauses', toggles(mark) == ['quiet'], env.commands(mark))
        window.close()
        pump(.4)
        env.service.execute('leave', {})
        settle(1.0)
        # After Tune in.
        window = env.launch(page='listen')
        radio_tune_buttons(window)[0].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None, 8)
        pump(.8)
        check('K14 after Tune in, Now shows and play has the focus', window.current_page() == 'now' and window.get_focus() is window.play,
              (window.current_page(), window.get_focus()))
        mark = len(env.log)
        key(window, 'space')
        settle(.8)
        check('K15 after Tune in Space pauses', toggles(mark) == ['quiet'], env.commands(mark))
        window.close()
        pump(.4)
        env.service.execute('leave', {})
        settle(1.0)
        # At launch with a last room but nothing active: the hero button has the focus, nothing plays by itself.
        mark = len(env.log)
        window = env.launch(page=None)
        pump(1.0)
        check('K16 at launch with only a last room, Rooms shows with the hero button focused and nothing started',
              window.current_page() == 'browse' and window.get_focus() is window.hero_button and starts(mark) == [],
              (window.current_page(), window.get_focus(), env.commands(mark)))

    # =========================================================================== E: plain-words errors (criterion 3)
    @section('E')
    def errors():
        raw_cases = ["[Errno 111] Connection refused: '/run/fixture/session.sock'",
                     'Traceback (most recent call last):\n  File "x.py", line 3\nKeyError: \'music\'',
                     "KeyError: 'music'", 'ValueError: invalid literal for int() with base 10',
                     'mpv exited: /nix/store/fixture-mpv/bin/mpv returned 2', '<urlopen error [Errno -2] Name or service not known>']
        bad = []
        for raw in raw_cases:
            head, details = next_model.plain_error(raw), next_model.error_details(raw)
            if any(part in head for part in ('Errno', 'Traceback', 'Error:', '/run/', '/nix/', 'urlopen', 'line 3')) or details != raw:
                bad.append((raw, head, details))
        check('E1 raw causes (errno, traceback, exception, paths) never become the headline and stay whole for Details', not bad, bad)
        plain = 'Choose a room first'
        check('E2 a plain controller sentence stays as it is, with no Details', next_model.plain_error(plain) == plain
              and next_model.error_details(plain) == '', (next_model.plain_error(plain), next_model.error_details(plain)))
        env.start_station_room()
        window = env.launch(page=None)
        pump(.6)
        raw = 'Traceback (most recent call last):\nKeyError: music at /srv/fixture/player.py line 3'
        real = session_client.request

        def failing(action, arguments=None, *rest, **kw):
            if action == 'layer':
                raise RuntimeError(raw)
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=failing):
            window.next_track()
            pump(1.2)
        shown = toast_texts(window)
        check('E3 a failed command toasts a plain sentence; the raw cause is not in the toast',
              shown and not any('KeyError' in t or '/srv/' in t or 'Traceback' in t for t in shown), shown)
        details = [b for b in widgets(window.toasts) if isinstance(b, Gtk.Button) and b.get_mapped() and b.get_label() == 'Details']
        check('E4 ... with a Details button', bool(details), shown)
        if details:
            details[0].emit('clicked')
            pump(.8)
            dialog = window.get_visible_dialog()
            body = dialog.get_body() if dialog is not None and hasattr(dialog, 'get_body') else ''
            check('E5 Details shows the raw cause', 'KeyError: music' in (body or ''), body)
            env.shot(window, 'acc-o9-e5-details')
            if dialog is not None:
                dialog.force_close()
                pump(.3)
        # A start the controller refuses: the card headline is plain, the raw cause is behind a collapsed Details.
        raw_start = "[Errno 111] Connection refused: '/run/fixture/stream.sock'"

        def refusing(action, arguments=None, *rest, **kw):
            if action in ('music', 'station.start', 'link', 'online'):
                raise OSError(raw_start)
            return real(action, arguments, *rest, **kw)
        window.close()
        pump(.3)
        env.service.execute('leave', {})
        settle(1.0)
        window = env.launch(page='listen')
        tune = radio_tune_buttons(window)
        with mock.patch.object(session_client, 'request', side_effect=refusing):
            tune[0].emit('clicked')
            wait(lambda: window.pending is not None and window.pending.get('state') == 'failed', 5)
        card = mapped_texts(window.pending_card)
        expander = getattr(window, 'pending_details', None)
        check('E6 a refused start says so plainly; no errno or path in the card headline/detail',
              any(t.startswith('Couldn’t start') for t in card) and not any(
                  'Errno' in t or '/run/' in t for t in (window.pending_title.get_text(), window.pending_detail.get_text())), card)
        check('E7 ... and the raw cause is behind a collapsed Details', expander is not None and expander.get_visible()
              and not expander.get_expanded() and 'Errno 111' in expander.text.get_text(), expander)
        env.shot(window, 'acc-o9-e7-start-refused')
        window.close()
        pump(.3)
        # A music layer that fails after the room is up: the Now banner is plain, the cause is behind Details.
        env.start_station_room()
        window = env.launch(page=None)
        pump(.6)
        env.start_error = "[Errno 2] No such file or directory: '/fixture/stream.pls'"
        item = next(source for source in window.radio if source.get('kind') == 'radio')
        try:
            env.service.execute('music', {'item': item})   # what Tune in on a Radio row sends
        except Exception as error:   # a synchronous refusal is fine too; the layer error is what Now must show
            print('music start refused: %s' % error)
        settle(2.0)
        pump(1.5)
        env.start_error = None
        details = getattr(window, 'banner_details', None)
        banner = mapped_texts(window.banner) - (mapped_texts(details) if details is not None else set())
        check('E8 a failed music layer: the Now banner says so without errno or path',
              window.banner.get_mapped() and banner and not any('Errno' in t or '/fixture/' in t for t in banner), sorted(banner))
        check('E9 ... and its raw cause is behind a collapsed Details', details is not None and details.get_visible()
              and not details.get_expanded() and 'Errno 2' in details.text.get_text(), details and details.text.get_text())
        env.shot(window, 'acc-o9-e9-music-failed')

    # =========================================================================== S: room service isn't running (criterion 3)
    @section('S')
    def offline():
        env.start_station_room()
        window = env.launch(page=None)
        pump(.6)
        calls = []
        active_before = copy.deepcopy(env.controller.active)
        with mock.patch.object(session_client, 'request', side_effect=ConnectionRefusedError(111, 'Connection refused')), \
                mock.patch.object(session_client, 'ensure_service', side_effect=lambda *a, **k: calls.append('ensure')):
            window.poll()
            pump(1.0)
            bar = getattr(window, 'offline_bar', None)
            check('S1 an unreachable room service shows a banner saying it isn’t running',
                  bar is not None and bar.get_reveal_child() and 'isn’t running' in window.offline_title.get_text(),
                  bar and window.offline_title.get_text())
            check('S2 the banner headline has no errno or exception text',
                  bar is not None and 'Errno' not in window.offline_title.get_text() and '111' not in window.offline_title.get_text())
            check('S3 Now does not look live: play is insensitive and the chip says Not connected',
                  not window.play.get_sensitive() and window.chip.get_text() == 'Not connected',
                  (window.play.get_sensitive(), window.chip.get_text()))
            env.shot(window, 'acc-o9-s-offline-now')
            deadline = time.monotonic() + 6.5
            while time.monotonic() < deadline:
                window.poll()
                pump(.5)
            check('S4 the banner is persistent (still there after 6.5 s of failed polls, unlike a toast)',
                  bar is not None and bar.get_reveal_child())
            mark = len(env.log)
            window.play.grab_focus()
            key(window, 'space')
            pump(.4)
            check('S5 Space does nothing while the service is down', toggles(mark) == [], env.commands(mark))
            start = getattr(window, 'offline_start', None)
            if start is not None:
                start.emit('clicked')
                start.emit('clicked')
                pump(1.2)
            check('S6 Start again calls ensure_service (once for a double click)', calls == ['ensure'], calls)
            check('S7 Start again sends no playback command', not [c for c in env.commands(mark) if c[0] != 'inspect'],
                  env.commands(mark))
        check('S8 nothing about the room changed while it was down', env.controller.active == active_before)
        pump(1.5)
        check('S9 the banner goes and Now is live again once the service answers',
              bar is not None and not bar.get_reveal_child() and window.play.get_sensitive(),
              bar and (bar.get_reveal_child(), window.play.get_sensitive()))
        with mock.patch.object(session_client, 'request', side_effect=ConnectionRefusedError(111, 'Connection refused')), \
                mock.patch.object(session_client, 'ensure_service', side_effect=session_client.SessionUnavailable('no graphical session')):
            window.poll()
            pump(.8)
            window.offline_start.emit('clicked')
            pump(1.0)
            check('S10 when Start again fails the banner stays and a toast says it still isn’t running',
                  window.offline_bar.get_reveal_child() and any('still isn’t running' in t for t in toast_texts(window)),
                  toast_texts(window))
        pump(1.5)
        # The service source: ensure_service only starts idle infrastructure (documented guarantee).
        import inspect
        src = inspect.getsource(session_client.ensure_service)
        check('S11 ensure_service sends no playback command (only inspect)', "'inspect'" in src
              and not any(word in src for word in ("'enter'", "'return'", "'resume'", "'music'")))

    # =========================================================================== D: fresh data (criterion 4)
    @section('D')
    def fresh():
        online = env.online_services
        connected = online.config.side_effect
        config_calls = []

        def unconnected():
            config_calls.append(threading.current_thread() is threading.main_thread())
            value = connected()
            value['jellyfin'] = {'endpoint': '', 'username': '', 'user_id': '', 'token_file': ''}
            return value

        def recorded():
            config_calls.append(threading.current_thread() is threading.main_thread())
            return connected()
        online.config.side_effect = unconnected
        config_file = Path(os.environ['XDG_CONFIG_HOME']) / 'listening-hub' / 'connections.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        if config_file.exists():
            config_file.unlink()
        favourites_file = hub.STATE / 'favorites.json'
        if favourites_file.exists():
            favourites_file.unlink()
        reads = []
        real_open = open

        def spy_open(file, *args, **kwargs):
            name = str(file)
            if name.endswith(('connections.json', 'favorites.json')) and threading.current_thread() is threading.main_thread():
                stack = ''.join(traceback.format_stack(limit=12))
                if 'next_' in stack and 'room_session.py' not in stack:
                    reads.append((name, stack[-600:]))
            return real_open(file, *args, **kwargs)
        snapshot = env.service.execute('inspect', {})
        check('D1 the snapshot carries a config generation', 'config_generation' in snapshot, sorted(snapshot)[:40])
        with mock.patch('builtins.open', side_effect=spy_open), mock.patch('io.open', side_effect=spy_open):
            window = env.launch(page='listen')
            pump(2.0)
            album = 'Neoclassical collection'
            check('D2 with no library connected, the library album is not shown', album not in mapped_texts(window.listen.index_body))
            online.config.side_effect = recorded
            config_file.write_text('{"note": "fixture connection, read through the patched config()"}')
            gen0 = snapshot.get('config_generation')
            gen1 = env.service.execute('inspect', {}).get('config_generation')
            check('D3 saving a library connection changes the config generation', gen0 != gen1, (gen0, gen1))
            check('D4 Library appears in the open window without a restart',
                  wait(lambda: album in mapped_texts(window.listen.index_body), 8), sorted(mapped_texts(window.listen.index_body))[:40])
            hub.save_favorite({'url': 'https://radio.example.test/acceptance-favourite', 'name': 'Acceptance Favourite FM'})
            radio_tune_buttons(window)
            check('D5 a World radio favourite saved elsewhere shows on the Radio shelf without a restart',
                  wait(lambda: 'Acceptance Favourite FM' in mapped_texts(window.listen.index_body), 8),
                  sorted(mapped_texts(window.listen.index_body))[:40])
            env.shot(window, 'acc-o9-d-favourite-on-radio')
        check('D6 no connections.json or favorites.json read on the GTK thread from window code', not reads, reads[:2])
        check('D7 the library config() is never called on the GTK thread', config_calls and not any(config_calls), config_calls)
        online.config.side_effect = connected

    # =========================================================================== L: the Listen-first path (criterion 6)
    @section('L')
    def listen_first():
        window = env.launch(page='listen')
        radio_tune_buttons(window)[0].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None, 8)
        pump(1.2)
        name = window.room_name.get_text()
        check('L1 Tune in with no room: Now is titled by what plays, not untitled', window.current_page() == 'now'
              and name and name.lower() not in ('your listening room', 'untitled', 'untitled room', 'listening room'), name)
        check('L2 ... and offers Choose a scene', getattr(window, 'choose_scene', None) is not None and window.choose_scene.get_mapped())
        window.kick_idle()
        path = env.shot(window, 'acc-o9-l1-tune-in-no-room')
        width, height = window.get_width(), window.get_height()
        light = brightness(path, (0, int(height * .1), width, int(height * .55)))
        check('L3 ... and the scene is not black (mean luminance of the upper scene area > 25)', light > 25, round(light, 1))
        check('L4 Back says it returns to Listen', window.back.get_tooltip_text() == 'Back to Listen', window.back.get_tooltip_text())
        key(window, 'Escape')
        check('L5 Esc in Now returns to Listen, where Tune in was pressed',
              window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'listen',
              (window.current_page(), window.browse.get_visible_child_name()))
        window.show_now()
        pump(.6)
        window.show_listen()
        pump(.6)
        window.set_focus(None)
        key(window, 'Escape')
        check('L6 Esc in Listen opened from Now goes back to Now', window.current_page() == 'now', window.current_page())
        window.show_rooms()
        pump(.4)
        window.show_listen()
        pump(.6)
        window.set_focus(None)
        key(window, 'Escape')
        check('L7 Esc in Listen opened from Rooms goes back to Rooms',
              window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms',
              (window.current_page(), window.browse.get_visible_child_name()))
        window.close()
        pump(.4)
        env.service.execute('leave', {})
        settle(1.0)
        # Enter a catalogue room, then Tune in something else: Changed, Save as my room, Back returns to Listen.
        window = env.launch(page='rooms')
        enter_first_room(window)
        check('L8 a room entered from Rooms: Back says Back to rooms', window.back.get_tooltip_text() == 'Back to rooms',
              window.back.get_tooltip_text())
        check('L9 an unchanged catalogue room is not marked Changed', not window.changed_chip.get_mapped())
        source = (env.controller.active.get('music') or {}).get('source_id')
        window.show_listen()
        buttons = radio_tune_buttons(window)
        rows = [b for b in buttons]
        rows[-1].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None, 8)
        pump(1.5)
        changed = (env.controller.active.get('music') or {}).get('source_id') != source
        check('L10 Tune in over the room changed its music (fixture sanity)', changed, env.controller.active.get('music'))
        check('L11 ... the room is marked Changed', window.changed_chip.get_mapped())
        save = [b for b in widgets(window.toasts) if isinstance(b, Gtk.Button) and b.get_mapped() and b.get_label() == 'Save as my room']
        check('L12 ... with Save as my room offered', bool(save), toast_texts(window))
        env.shot(window, 'acc-o9-l2-changed')
        if save:
            save[0].emit('clicked')
            pump(.8)
            dialog = window.get_visible_dialog()
            check('L13 Save as my room opens the save dialog', dialog is not None and getattr(dialog, 'get_heading', lambda: '')() == 'Save as my room',
                  dialog)
            if dialog is not None:
                dialog.force_close()
                pump(.3)
        window.back.emit('clicked')
        pump(.6)
        check('L14 Back after Tune in returns to Listen', window.current_page() == 'browse'
              and window.browse.get_visible_child_name() == 'listen', (window.current_page(), window.browse.get_visible_child_name()))
        window.close()
        pump(.4)
        env.service.execute('leave', {})
        settle(1.0)
        # Listen first, then Save as my room with the offered name, then Tune in again later: never black or untitled.
        # Fixture: a listener whose first action is Tune in (no last room), so the room really is ad hoc.
        with env.controller.lock:
            env.controller.last_session = None
        window = env.launch(page='listen')
        radio_tune_buttons(window)[0].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None, 8)
        pump(1.0)
        window.save_dialog()
        pump(.6)
        dialog = window.get_visible_dialog()
        offered = window.save_entry.get_text() if dialog is not None else None
        check('L15a fixture: Tune in with no room and no last room made an ad-hoc room',
              (env.controller.active or {}).get('room_id') is None, env.controller.active and env.controller.active.get('id'))
        if dialog is not None:
            dialog.emit('response', 'save')
            dialog.force_close()
            settle(1.5)
            pump(2.0)

        def untitled_or_black(tag):
            title = window.room_name.get_text()
            window.kick_idle()
            path = env.shot(window, 'acc-o9-' + tag)
            light = brightness(path, (0, int(window.get_height() * .1), window.get_width(), int(window.get_height() * .55)))
            return (title.lower() in ('your listening room', 'untitled', 'listening room', '') or light <= 25), (title, round(light, 1))
        bad, seen = untitled_or_black('l3-saved-listen-first-room')
        check('L15 right after Save as my room (offered name %r) Now is neither black nor untitled' % offered, not bad, seen)
        env.service.execute('leave', {})
        settle(1.0)
        window.close()
        pump(.3)
        window = env.launch(page='listen')
        radio_tune_buttons(window)[0].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None, 8)
        pump(1.0)
        bad, seen = untitled_or_black('l4-tune-in-after-saved-room')
        check('L16 a later Tune in with no room active (last room was that saved one) is neither black nor untitled', not bad, seen)
        # Strengthened after the repair (2026-11-20): the offered name itself is not generic, and a saved room
        # with a real name but no scene keeps its name and still is not black.
        check('L15b Save as my room offers what plays, not a generic name', offered and not next_model.generic_name(offered)
              if hasattr(next_model, 'generic_name') else offered not in (None, '', 'Your listening room'), offered)
        env.service.execute('save', {'name': 'Acceptance Den'})
        settle(1.5)
        pump(1.5)
        title = window.room_name.get_text()
        bad, seen = untitled_or_black('l5-named-saved-room-without-scene')
        check('L17 a saved room with its own name and no scene shows that name over a scene that is not black',
              title == 'Acceptance Den' and seen[1] > 25, seen)

    # =========================================================================== H: Hyprland placement (criterion 7)
    @section('H')
    def hyprland():
        check('H1 Hyprland is detected from the session signature', next_model.hyprland({'HYPRLAND_INSTANCE_SIGNATURE': 'x'})
              and not next_model.hyprland({}) and not next_model.hyprland({'HYPRLAND_INSTANCE_SIGNATURE': ''}))
        base = Path(env.folder) / 'acceptance-placement'

        def controller_with(settings, last=None):
            folder = base / str(time.monotonic_ns())
            folder.mkdir(parents=True)
            stored = {'settings': settings} if settings is not None else {}
            if last is not None:
                stored['last_session'] = last
            (folder / 'session.json').write_text(json.dumps(stored))
            return room_session.Controller(env.backend.__class__(), folder), folder
        cases = [(None, 'here'), ({'placement': '9'}, 'here'), ({'placement': 'pin'}, 'pin'),
                 ({'placement': '9', 'placement_chosen': True}, '9'), ({'placement': 'here'}, 'here')]
        got = []
        for settings, want in cases:
            controller, _folder = controller_with(settings)
            got.append((settings, controller.settings.get('placement'), want))
            controller.close()
        check('H2 default Here; a stored implicit 9 moves to Here; explicit and non-default choices are kept',
              all(have == want for _s, have, want in got), got)
        controller, folder = controller_with({'placement': '9'})
        controller.apply_settings({'placement': '9'})
        controller.close()
        again = room_session.Controller(env.backend.__class__(), folder)
        check('H3 choosing Workspace 9 explicitly survives a restart', again.settings.get('placement') == '9', again.settings)
        again.close()
        refused = False
        try:
            env.service.execute('settings', {'values': {'placement_chosen': True}})
        except Exception as error:
            refused = 'Unknown room setting' in str(error)
        check('H4 the internal placement mark cannot be set through the settings command', refused)
        # Entering a catalogue room with default settings opens the scene Here.
        window = env.launch(page='rooms')
        enter_first_room(window)
        placement = (env.backend.state.get('picture') or {}).get('placement')
        check('H5 a room entered with default settings opens its scene Here, not on workspace 9', placement == 'here',
              env.backend.state.get('picture'))
        # Without Hyprland the options say they need it (coordinator decision a), in the Mix and Preferences.
        window.open_mix()
        pump(1.0)
        note = next_model.NO_HYPRLAND
        check('H6 without Hyprland the Mix says placement needs Hyprland', note in mapped_texts(window.mix.rows['picture']),
              sorted(mapped_texts(window.mix.rows['picture'])))
        env.shot(window, 'acc-o9-h-mix-no-hyprland')
        window.sheet.set_open(False)
        pump(.4)
        window.open_preferences()
        pump(.8)
        prefs = window.preferences
        prefs.set_visible_page(prefs.pages['scene'])
        pump(.6)
        check('H7 without Hyprland Preferences says placement needs Hyprland', prefs.placement_row.get_subtitle() == note,
              prefs.placement_row.get_subtitle())
        check('H8 Preferences shows Here as the current default', prefs.placement_row.get_selected() == 1, prefs.placement_row.get_selected())
        prefs.close()
        pump(.4)
        window.close()
        pump(.4)
        os.environ['HYPRLAND_INSTANCE_SIGNATURE'] = 'acceptance-fixture-not-a-session'
        try:
            window = env.launch(page=None)
            window.open_mix()
            pump(1.0)
            check('H9 in a Hyprland session the Mix shows no needs-Hyprland note', note not in mapped_texts(window.mix.rows['picture']))
            window.sheet.set_open(False)
            pump(.3)
            window.close()
            pump(.3)
        finally:
            os.environ.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
        # Returning to a last room recorded while 9 was the implicit default.
        env.service.execute('leave', {})
        settle(1.0)
        last = copy.deepcopy(env.controller.last_session)
        if last:
            last.setdefault('picture', {})['placement'] = '9'
            last['picture']['enabled'] = True
            controller, folder = controller_with({'placement': '9'}, last)
            controller._home_action = lambda *_: None
            controller._route_preferred = lambda: None
            check('H10 an old implicit 9 is migrated in settings (fixture sanity)', controller.settings['placement'] == 'here')
            import session_service
            service = session_service.Service(controller)
            try:
                service.execute('return', {})
                deadline = time.monotonic() + 8
                while (controller.transition or not controller.active) and time.monotonic() < deadline:
                    controller.refresh()
                    time.sleep(.1)
            except Exception as error:
                print('return failed: %s' % error)
            opened = (controller.backend.state.get('picture') or {}).get('placement')
            check('H11 Return to a last room saved under the old implicit default opens the scene Here (not workspace 9)',
                  opened == 'here', 'scene opened with placement=%r; last_session placement=%r' % (
                      opened, (controller.last_session or {}).get('picture', {}).get('placement')))
            controller.close()
        # Strengthened after the repair (2026-11-20): saved rooms migrate too, once, and explicit choices are kept.
        def prepared(stored, variants=None):
            folder = base / ('m%d' % time.monotonic_ns())
            folder.mkdir(parents=True)
            (folder / 'session.json').write_text(json.dumps(stored))
            if variants is not None:
                (folder / 'room-variants.json').write_text(json.dumps(variants))
            return folder

        def boot(folder):
            controller = room_session.Controller(env.backend.__class__(), folder)
            state = (dict(controller.settings), copy.deepcopy(controller.last_session), controller.variants())
            controller.close()
            return state
        recipe = copy.deepcopy(last) if last else {'id': 'r', 'room_id': None, 'name': 'Old room',
                                                    'music': {'source_id': 'x'}, 'picture': {'enabled': True}}
        recipe.setdefault('picture', {})['placement'] = '9'
        variant = dict(copy.deepcopy(recipe), id='variant-acceptance', name='Old saved room')
        folder = prepared({'version': 1, 'settings': {'placement': '9'}, 'last_session': recipe}, [variant])
        settings, last_room, variants = boot(folder)
        check('H12 an implicit 9 in the last room and in saved rooms moves to Here on upgrade',
              last_room['picture']['placement'] == 'here' and variants[0]['picture']['placement'] == 'here',
              (last_room['picture'], [v['picture'] for v in variants]))
        stored = json.loads((folder / 'session.json').read_text())
        stored['last_session']['picture']['placement'] = '9'      # chosen later, per room (the Mix toggle)
        (folder / 'session.json').write_text(json.dumps(stored))
        later = json.loads((folder / 'room-variants.json').read_text())
        later[0]['picture']['placement'] = '9'
        (folder / 'room-variants.json').write_text(json.dumps(later))
        settings, last_room, variants = boot(folder)
        check('H13 the migration runs once: a 9 chosen after it survives the next restart',
              last_room['picture']['placement'] == '9' and variants[0]['picture']['placement'] == '9',
              (last_room['picture'], [v['picture'] for v in variants]))
        folder = prepared({'version': 1, 'settings': {'placement': 'pin', 'placement_chosen': True}, 'last_session': recipe},
                          [variant])
        settings, last_room, variants = boot(folder)
        check('H14 with a placement chosen before the upgrade (legacy mark) recipes are not touched and the choice stays',
              settings['placement'] == 'pin' and last_room['picture']['placement'] == '9'
              and variants[0]['picture']['placement'] == '9', (settings, last_room['picture']))
        check('H15 the legacy mark leaves the settings the snapshot shows', 'placement_chosen' not in settings, settings)
        settings, _last, _v = boot(folder)
        check('H16 ... and the choice survives a second restart after the mark moved', settings['placement'] == 'pin', settings)
        check('H17 the running controller\'s snapshot settings carry no internal placement bookkeeping',
              not any(k.startswith('placement_') for k in env.service.execute('inspect', {}).get('settings', {})),
              env.service.execute('inspect', {}).get('settings'))

    # =========================================================================== P: polling (O6 follow-up repair)
    @section('P')
    def polling():
        env.start_station_room()
        window = env.launch(page=None)
        pump(.8)
        real = session_client.request

        def slow(action, arguments=None, *rest, **kw):
            if action == 'inspect':
                time.sleep(1.0)
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=slow):
            pump(1.5)
            mark = len(env.log)
            for _ in range(30):
                window.poll()
            serial = getattr(window, 'polls_sent', 0)
            pump(4.0)
            inspects = len([a for a, _ in env.log[mark:] if a == 'inspect'])
        check('P1 thirty poll requests during slow reads are coalesced: at most one read in flight, bounded re-sends',
              1 <= inspects <= 6, inspects)
        check('P2 a poll requested while one was in flight is answered by a later read',
              hasattr(window, 'fresh_since') and window.fresh_since(serial), (serial, getattr(window, 'snapshot_poll', None)))
        pump(2.0)
        mark = len(env.log)
        pump(3.0)
        idle = len([a for a, _ in env.log[mark:] if a == 'inspect'])
        check('P3 with fast reads and no requests, polling stays at the timer rate (no back-to-back re-sends)',
              idle <= int(3.0 / (next_window_module.POLL_MS / 1000.)) + 2, (idle, next_window_module.POLL_MS))
        with mock.patch.object(session_client, 'request', side_effect=ConnectionRefusedError(111, 'Connection refused')):
            sent_before = getattr(window, 'polls_sent', 0)   # a read already in flight may still land fresh
            for _ in range(10):
                window.poll()
            pump(2.0)
            check('P4 failed reads never mark the shown snapshot fresh (nothing sent after the failure began counts)',
                  getattr(window, 'snapshot_poll', 0) <= sent_before, (sent_before, getattr(window, 'snapshot_poll', None)))
        pump(1.5)
        check('P5 polling recovers after failures', window.offline_bar is not None and not window.offline_bar.get_reveal_child())
        # A quick station choice whose save is slower than the page's 3 s pending timeout (a busy controller):
        # the page must not fall back to the pre-save station meanwhile, or a next choice builds on it (o3 4p).
        import next_station
        window.close()
        pump(.3)
        window = env.launch(page='listen')
        station = next(item for item in env.controller.stations() if item['name'] == 'Morning lift')
        window.listen.show_station(station)
        pump(.6)
        mood = next(m for m in next_station.MOODS if m['key'] == 'energy')
        before = next_station.choices(window.listen.station)['energy']
        index = next(i for i, (text, _v) in enumerate(mood['choices']) if text != before)
        chosen = mood['choices'][index][0]

        def slow_save(action, arguments=None, *rest, **kw):
            if action == 'station.save':
                time.sleep(4.0)
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=slow_save):
            window.listen.mood_groups['energy'].set_active(index)
            seen = []
            deadline = time.monotonic() + 3.8
            while time.monotonic() < deadline:
                pump(.1)
                seen.append(next_station.choices(window.listen.station)['energy'])
            pump(1.5)
        settle(.5)
        check('P6 while a slow save (4 s) is in flight the page keeps the chosen value, never the pre-save one',
              seen and all(value == chosen for value in seen), sorted(set(seen)) + ['chosen=' + chosen])
        stored = next_station.choices(next(item for item in env.controller.stations() if item['id'] == station['id']))
        check('P7 ... and the slow save lands', stored['energy'] == chosen, stored)
        # Added 2026-11-20 after the pending timer was removed: a save the controller never answers must still end.
        # A real unix socket that accepts and never replies, reached through the real session_client.request.
        import socket as socket_module
        import tempfile as tempfile_module
        original_request = env.patches[0].temp_original
        silent_dir = tempfile_module.mkdtemp(prefix='acc-silent-')
        silent_path = os.path.join(silent_dir, 'silent.sock')
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(silent_path)
        server.listen(4)
        accepted = []
        threading.Thread(target=lambda: [accepted.append(server.accept()) for _ in range(4)], daemon=True).start()
        before = next_station.choices(window.listen.station)['energy']
        index = next(i for i, (text, _v) in enumerate(mood['choices']) if text != before)

        def silent(action, arguments=None, *rest, **kw):
            if action == 'station.save':
                with mock.patch.object(session_client, 'socket_path', return_value=silent_path):
                    return original_request(action, arguments, *rest, **kw)
            return real(action, arguments, *rest, **kw)
        started = time.monotonic()
        with mock.patch.object(session_client, 'request', side_effect=silent):
            window.listen.mood_groups['energy'].set_active(index)
            ended = wait(lambda: window.listen.pending_station is None, 20)
        took = time.monotonic() - started
        pump(1.0)
        server.close()
        check('P8 a save the controller never answers ends (bounded by the request timeout) instead of staying pending',
              ended and took < 15, round(took, 1))
        stored = next_station.choices(next(item for item in env.controller.stations() if item['id'] == station['id']))
        check('P9 ... the page then shows what is really stored, with a plain failure toast',
              next_station.choices(window.listen.station)['energy'] == stored['energy']
              and toast_texts(window) and not any('unavailable:' in t or 'timed out' in t for t in toast_texts(window)),
              (next_station.choices(window.listen.station)['energy'], stored['energy'], toast_texts(window)))

    # =========================================================================== W: widths (criterion 8)
    @section('W')
    def widths():
        env.start_station_room()
        for width in (360, 640, 900):
            window = env.launch(width=width, height=720, page=None)
            window.show_now()
            pump(1.0)
            window.kick_idle()
            got = window.get_width()
            check('W1 the window really is %d px wide' % width, abs(got - width) <= 2, got)
            page = window.current_page()
            root = window.mini_player if page == 'mini' else window.controls
            controls = [w for w in widgets(root) if isinstance(w, (Gtk.Button, Gtk.Scale, Gtk.Image, Gtk.MenuButton))
                        and w.get_mapped() and w.get_width() > 0]
            boxes = [(w, bounds(w, window)) for w in controls]
            clashes = [(type(a).__name__, a.get_tooltip_text() or getattr(a, 'get_label', lambda: '')(), ra,
                        type(b).__name__, b.get_tooltip_text() or getattr(b, 'get_label', lambda: '')(), rb)
                       for i, (a, ra) in enumerate(boxes) for b, rb in boxes[i + 1:]
                       if overlaps(ra, rb) and not inside(a, b) and not inside(b, a)]
            check('W2 at %d px no two controls in the %s control row overlap' % (width, page), not clashes, clashes[:3])
            outside = [(type(w).__name__, r) for w, r in boxes if r and (r[0] < -1 or r[0] + r[2] > got + 1)]
            check('W3 at %d px every control is inside the window' % width, not outside, outside[:3])
            window.toast('An acceptance toast at %d px' % width)
            pump(.9)
            play = bounds(window.mini_player.play if page == 'mini' else window.play, window)
            tboxes = [bounds(t, window) for t in toast_widgets(window)]
            check('W4 at %d px the toast is shown and does not cover play' % width, tboxes and not any(overlaps(t, play) for t in tboxes),
                  (tboxes, play))
            env.shot(window, 'acc-o9-w-now-%dpx' % width)
            if page == 'now':
                size = (window.get_width(), window.get_height())
                window.open_mix()
                pump(1.2)
                window.toast('An acceptance toast over the Mix at %d px' % width)
                pump(.9)
                rows = [r for r in window.mix.rows.values() if r.get_mapped()]
                last = max(rows, key=lambda r: bounds(r, window)[1]) if rows else None
                tboxes = [bounds(t, window) for t in toast_widgets(window)]
                check('W5 at %d px the toast covers no part of the Mix\'s last row' % width,
                      last is not None and tboxes and not any(overlaps(t, bounds(last, window)) for t in tboxes),
                      (tboxes, last and bounds(last, window)))
                covered = [b.get_label() or b.get_tooltip_text() for b in widgets(window.mix) if isinstance(b, Gtk.Button)
                           and b.get_mapped() and any(overlaps(t, bounds(b, window)) for t in tboxes)]
                check('W5b at %d px the toast covers no Mix control (header included)' % width, not covered, covered)
                env.shot(window, 'acc-o9-w-mix-%dpx' % width)
                pump(3.0)
                check('W6 at %d px the window size is stable with the Mix and a toast open (no growth loop)' % width,
                      (window.get_width(), window.get_height()) == size, ((window.get_width(), window.get_height()), size))
                window.sheet.set_open(False)
            window.close()
            pump(1.0)
        # Strengthened after the repair (2026-11-20): a short window with the Mix open keeps the toast on screen.
        window = env.launch(width=640, height=520, page=None)
        window.show_now()
        pump(.8)
        window.open_mix()
        pump(1.2)
        window.toast('An acceptance toast in a short window')
        pump(.9)
        size = (window.get_width(), window.get_height())
        tboxes = [bounds(t, window) for t in toast_widgets(window)]
        check('W9 in a short 640x520 window with the Mix open the toast is fully on screen',
              tboxes and all(t[1] >= 0 and t[1] + t[3] <= size[1] for t in tboxes), (tboxes, size))
        pump(3.0)
        check('W10 ... and the window does not grow', (window.get_width(), window.get_height()) == size,
              ((window.get_width(), window.get_height()), size))
        window.sheet.set_open(False)
        window.close()
        pump(.6)
        env.service.execute('leave', {})
        settle(1.0)
        window = env.launch(width=360, height=640, page=None)
        pump(1.0)
        found = {b.get_label() or '': bounds(b, window) for b in widgets(window.mini_player)
                 if isinstance(b, Gtk.Button) and b.get_mapped() and (b.get_label() or '')}
        ret = [r for k, r in found.items() if k.startswith('Return')]
        browse = found.get('Browse rooms')
        check('W7 the idle mini layout at 360 px offers Return and Browse rooms, side by side without overlap, inside the window',
              ret and browse and not overlaps(ret[0], browse) and browse[0] + browse[2] <= 361 and ret[0][0] >= -1, found)
        env.shot(window, 'acc-o9-w-idle-mini-360px')
        mark = len(env.log)
        env.press(window.mini_player, 'Browse rooms')
        pump(.8)
        check('W8 Browse rooms shows Rooms and starts nothing', window.current_page() == 'browse'
              and window.browse.get_visible_child_name() == 'rooms' and starts(mark) == [], (window.current_page(), env.commands(mark)))

    # =========================================================================== O: O6 follow-ups (criterion 9)
    @section('O')
    def o6_follow_ups():
        window = env.launch(page='listen')
        buttons = radio_tune_buttons(window)
        real = session_client.request

        def slow(action, arguments=None, *rest, **kw):
            if action in ('music', 'link', 'station.start', 'online'):
                time.sleep(2.5)   # the controller answers after the (shortened) grace time
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(next_window_module, 'START_GRACE_S', 1.0), \
                mock.patch.object(session_client, 'request', side_effect=slow):
            window.toast('A five second toast that is already showing')
            pump(.3)
            mark = len(env.log)
            buttons[0].emit('clicked')
            pump(.3)
            buttons[-1].emit('clicked')   # a different station, pressed while the first connects
            pump(.4)
            shown = toast_texts(window)
            refused = any('connecting' in t.lower() for t in shown)
            check('O1 a different start while one connects is refused with a visible toast, not silently ignored', refused, shown)
            check('O2 ... and that refusal is visible at once, ahead of the 5 s toast already showing',
                  refused and 'A five second toast that is already showing' not in shown, shown)
            wait(lambda: window.pending is not None and window.pending.get('state') == 'failed', 3)
            check('O3 a slow start shows the taking-longer card after the grace time',
                  window.pending is not None and window.pending.get('state') == 'failed', window.pending)
            check('O4 ... with Try again insensitive while the first start is still running',
                  window.pending is not None and not window.pending_retry.get_sensitive())
            window.pending_retry.emit('clicked')
            window.retry_pending()
            wait(lambda: window.pending is None, 6)
            settle(1.0)
        check('O5 the late success clears the failure card', window.pending is None and env.controller.active is not None,
              window.pending)
        check('O6 exactly one start reached the controller (no double start, the refused one not sent)', len(starts(mark)) == 1,
              starts(mark))
        window.close()
        pump(.3)
        env.service.execute('leave', {})
        settle(1.0)
        # A refused start: Try again sends exactly one new start, even when pressed repeatedly.
        window = env.launch(page='listen')
        tune = radio_tune_buttons(window)
        refuse = [True]

        def refusing(action, arguments=None, *rest, **kw):
            if action in ('music', 'link', 'station.start', 'online') and refuse[0]:
                raise OSError('The stream could not be opened')
            if action in ('music', 'link', 'station.start', 'online'):
                time.sleep(1.0)
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=refusing):
            tune[0].emit('clicked')
            wait(lambda: window.pending is not None and window.pending.get('state') == 'failed', 5)
            refuse[0] = False
            mark = len(env.log)
            window.pending_retry.emit('clicked')
            window.pending_retry.emit('clicked')
            window.retry_pending()
            pump(.2)
            wait(lambda: window.pending is None, 6)
            settle(1.0)
        check('O7 Try again after a failure sends exactly one start even when pressed repeatedly', len(starts(mark)) == 1, starts(mark))
        # Failure toasts are urgent too.
        real = session_client.request

        def failing(action, arguments=None, *rest, **kw):
            if action == 'layer':
                raise RuntimeError('The player could not open the stream')
            return real(action, arguments, *rest, **kw)
        window.toast('Another five second toast')
        pump(.3)
        with mock.patch.object(session_client, 'request', side_effect=failing):
            window.next_track()
            pump(1.0)
        shown = toast_texts(window)
        check('O8 a failure toast is shown at once, not queued behind a visible 5 s toast',
              'The player could not open the stream' in shown and 'Another five second toast' not in shown, shown)
        # The explicit Ticket channel.
        import glob
        sources = ''.join(Path(p).read_text() for p in glob.glob(str(env.source / 'next_*.py')))
        check('O9 the implicit _watch channel is gone (no _watch / watch_next_command / unwatch_command)',
              not any(word in sources for word in ('_watch', 'watch_next_command', 'unwatch_command')))
        import next_actions
        ticket = window.command('inspect', {})
        check('O10 command() hands back an explicit Ticket', isinstance(ticket, getattr(next_actions, 'Ticket', ())), ticket)
        window.close()
        pump(.3)
        env.service.execute('leave', {})
        settle(1.0)
        # The explicit channel: each Ticket finishes with its own command, never with whichever ends first.
        window = env.launch(page=None)
        real = session_client.request

        def slow(action, arguments=None, *rest, **kw):
            if action == 'layer':
                time.sleep(1.5)
                raise RuntimeError('The player refused')
            return real(action, arguments, *rest, **kw)
        ended = []
        with mock.patch.object(session_client, 'request', side_effect=slow):
            first = window.command('layer', {'layer': 'music', 'action': 'next'}, quiet=True)
            second = window.command('inspect', {})
            first.add_finish(lambda: ended.append('slow'))
            second.add_finish(lambda: ended.append('fast'))
            pump(.6)
            early = list(ended)
            pump(1.8)
        check('O11 a Ticket finishes with its own command: the fast one first, the slow one only when it ends (even failing)',
              early == ['fast'] and ended == ['fast', 'slow'], (early, ended))
        late = []
        second.add_finish(lambda: late.append(1))
        check('O12 a finish callback added after the command ended runs at once', late == [1], late)
        window.close()
        pump(.3)

    # =========================================================================== Q: quick wins (criterion 10)
    @section('Q')
    def quick_wins():
        window = env.launch(page='rooms')
        entries = list(window.rooms.card_for.values())
        window.rooms.open_preview(entries[0])
        pump(.8)
        page = window.rooms.preview_widgets['page']
        said = [t for t in mapped_texts(page) if 'until you enter' in t or 'Nothing starts' in t]
        check('Q1 the room preview says nothing plays until you enter exactly once', len(said) == 1, said)
        window.rooms.close_preview(animate=False)
        pump(.3)
        check('Q2 Timer is insensitive with no room active', not window.timer_button.get_sensitive())
        window.show_listen()
        pump(.6)
        scope_titles = [t for _k, t in window.listen.scope_list] + [t for _k, t in getattr(__import__('next_listen_more'), 'MORE_SCOPES', ())]
        check('Q3 the Remembered scope is called History', 'History' in scope_titles and 'Remembered' not in scope_titles, scope_titles)
        window.close()
        pump(.3)
        env.start_station_room()
        window = env.launch(page=None)
        pump(.6)
        check('Q4 Timer is sensitive with a room active', window.timer_button.get_sensitive())
        labelled = []
        original = Gtk.Widget.update_property

        # Accessible labels are write-only in GTK; read them back by building a fresh row and recording the calls.
        def record(self, properties, values):
            labelled.append((self, list(properties), list(values)))
            return original(self, properties, values)
        Gtk.Widget.update_property = record
        try:
            window.close()
            pump(.3)
            window = env.launch(page=None)
            pump(.6)
        finally:
            Gtk.Widget.update_property = original
        placement = window.mix.rows['picture'].placement
        names = [values for widget, props, values in labelled if widget is placement and Gtk.AccessibleProperty.LABEL in props]
        check('Q5 the Mix placement group has an accessible label', bool(names), names)
        # Forget… with nothing learned.
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        window.show_listen()
        pump(.4)
        window.listen.show_station(station)
        pump(1.0)
        check('Q6 Forget… is insensitive when the station has learned nothing', not window.listen.forget_button.get_sensitive())
        env.controller.station_memory[station['id']] = {'liked_artists': ['Fixture Artist'], 'disliked': [], 'reset_at': -1}
        env.controller.refresh()
        pump(1.2)
        window.listen.show_station(station)
        pump(1.0)
        check('Q7 Forget… is sensitive once the station has learned something', window.listen.forget_button.get_sensitive())

    # =========================================================================== M: main-loop hazards
    @section('M')
    def main_loop():
        env.start_station_room()
        window = env.launch(page=None)
        pump(1.0)
        calls = []
        real_idle = GLib.idle_add

        def counting(function, *args, **kwargs):
            def wrapped(*a):
                result = function(*a)
                calls.append((getattr(function, '__qualname__', repr(function)), bool(result)))
                return False   # recorded; never re-armed here, so a truthy callback fails the check instead of spinning
            return real_idle(wrapped, *args, **kwargs)
        with mock.patch.object(GLib, 'idle_add', side_effect=counting):
            window.toast('One toast')
            window.show_rooms()
            pump(.4)
            window.show_now()
            pump(.4)
            window.error_toast(RuntimeError('Traceback: fixture'))
            window.run_local(lambda: None, 'A local action finished')   # test sound and system volume take this path
            window.run_local(lambda: 1 / 0, None)
            window.poll()
            pump(2.0)
        truthy = [name for name, result in calls if result]
        check('M1 no idle callback returned a truthy value (each runs once) during toasts, navigation and polls',
              not truthy, truthy[:6])
        size = (window.get_width(), window.get_height())
        window.open_mix()
        pump(4.0)
        check('M2 the window does not grow by itself with the Mix open', (window.get_width(), window.get_height()) == size,
              ((window.get_width(), window.get_height()), size))

    failed = [name for name, ok in checks if not ok]
    (env.output / 'acceptance-o9-ux.json').write_text(json.dumps({'checks': checks}, indent=1))
    print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)))
