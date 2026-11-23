"""O11-CONTROL evidence: the Mix keys and row buttons, the Mix while connecting (honest Cancel), and the
popover's per-layer line.

Real GTK on private Xvfb, the session controller on its test double (see o6_env): nothing reaches the
network, the desktop session or a real player. Prints PASS/FAIL lines and writes screenshots.

  scripts/listening-dev.sh probe OUT listening-o11c-ui
"""
import time

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o11c-ui')
checks = []
RAIN = {'kind': 'local', 'source_id': 'rain-glass', 'volume': 20, 'muted': False, 'window_open': 35}


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]), flush=True)


def wait(predicate, timeout=5.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        env.pump(.1)
    return bool(predicate())


def status():
    layers = env.controller.snapshot().get('layers') or {}
    return {name: (layers.get(name) or {}).get('status', 'Stopped') for name in ('music', 'atmosphere')}


def enter(room_id='northern-quiet', ambience=True):
    if env.controller.active:
        env.service.execute('leave', {'fade_seconds': 0})
        env.settle(.4)
    args = {'room_id': room_id}
    if ambience:
        args['atmosphere'] = dict(RAIN)
    env.service.execute('enter', args)
    wait(lambda: env.controller.active and not env.controller.transition and status()['music'] == 'Playing', 8)
    env.settle(.4)


def shown(widget):
    return widget.get_visible() and widget.get_mapped()


with env:
    Gtk, Gdk = env.Gtk, env.Gdk
    import next_mini
    enter()
    window = env.launch(page=None)
    window.show_now()
    env.pump(.8)
    window.open_mix()
    env.pump(1.0)
    mix = window.mix
    env.shot(window, 'o11c-01-mix')

    # ---- keys -------------------------------------------------------------------------------------
    mark = len(env.log)
    handled = mix.keys.emit('key-pressed', Gdk.KEY_m, 0, 0)
    env.settle(.5)
    check('K1 M in the Mix pauses Music only', handled and status() == {'music': 'Paused', 'atmosphere': 'Playing'},
          (handled, status(), env.commands(mark)))
    handled = mix.keys.emit('key-pressed', Gdk.KEY_a, 0, 0)
    env.settle(.5)
    check('K2 A in the Mix pauses Ambience', handled and status()['atmosphere'] == 'Paused', status())
    mix.keys.emit('key-pressed', Gdk.KEY_m, 0, 0)
    mix.keys.emit('key-pressed', Gdk.KEY_a, 0, 0)
    env.settle(.5)
    check('K3 M and A again play them', status() == {'music': 'Playing', 'atmosphere': 'Playing'}, status())
    scale = mix.rows['atmosphere'].scale
    before = scale.get_value()
    scale.grab_focus()
    env.pump(.2)
    mark = len(env.log)
    handled = mix.keys.emit('key-pressed', Gdk.KEY_Up, 0, Gdk.ModifierType.SHIFT_MASK)
    env.pump(.5)
    sent = [c for c in env.commands(mark) if c[0] == 'layer' and c[1].get('action') == 'volume']
    check('K4 Shift+Up raises the focused row\'s level by 5 (one debounced command, that layer only)',
          handled and scale.get_value() == min(100, before + 5) and len(sent) == 1 and sent[0][1]['layer'] == 'atmosphere',
          (before, scale.get_value(), sent))
    mix.rows['music'].halt.grab_focus()
    env.pump(.2)
    level = mix.rows['music'].scale.get_value()
    mix.keys.emit('key-pressed', Gdk.KEY_Down, 0, Gdk.ModifierType.SHIFT_MASK)
    env.pump(.5)
    check('K5 Shift+Down on a Music row button lowers Music', mix.rows['music'].scale.get_value() == max(0, level - 5),
          (level, mix.rows['music'].scale.get_value()))
    window.sheet.set_open(False)
    env.pump(.6)
    mark = len(env.log)
    check('K6 M does nothing while the Mix is closed', not mix.keys.emit('key-pressed', Gdk.KEY_m, 0, 0)
          and not env.commands(mark), env.commands(mark))
    check('K7 the Mix keys are one list for the shortcut table',
          [k for _a, k, _ in __import__('next_mix').MIX_SHORTCUTS] == ['M', 'A', 'Shift+Up / Down'])

    # ---- row buttons (N10) ------------------------------------------------------------------------
    window.open_mix()
    env.pump(.8)
    music = mix.rows['music']
    check('R1 live radio: Stop is on the row, no Next track button', shown(music.halt) and not shown(music.skip))
    check('R2 the ⋯ menu never holds a single item', not shown(music.menu) or sum(
        1 for item in (music.next_item, music.retry_item, music.change_item, music.stop_item) if item.get_visible()) > 1)
    import copy
    tracks = copy.deepcopy(o6_env.TRACKS)
    env.service.execute('music', {'item': {'name': 'Neoclassical collection', 'kind': 'radio', 'reference': tracks[0],
                                           'queue': tracks, 'queue_index': 0}})
    wait(lambda: env.controller.active and not env.controller.transition and status()['music'] == 'Playing', 8)
    env.settle(.8)
    check('R3 a library queue: Next track is a button on the Music row', shown(music.skip), music.skip.get_visible())
    mark = len(env.log)
    music.skip.emit('clicked')
    env.settle(.4)
    check('R4 the row\'s Next track sends one layer next', [c for c in env.commands(mark)][:1] ==
          [('layer', {'layer': 'music', 'action': 'next'})], env.commands(mark))
    env.shot(window, 'o11c-01b-mix-library')
    window.sheet.set_open(False)
    env.pump(.4)
    window.close()
    env.pump(.4)

    # ---- connecting: progress and an honest Cancel ------------------------------------------------
    enter('northern-quiet', ambience=True)
    window = env.launch(page=None)
    env.pump(.6)
    env.start_delay = 4.
    mark = len(env.log)
    target = next(e for e in window.rooms.card_for.values() if e.get('name') != 'Northern quiet' and e.get('id'))
    window.start('enter', {'room_id': target['id']}, target['name'])
    env.pump(.6)
    mix = window.mix
    window.open_mix()
    env.pump(.8)
    rows_shown = [name for name, row in mix.rows.items() if shown(row)]
    check('C1 while connecting the Mix shows only progress and Cancel',
          shown(mix.connecting) and shown(mix.cancel_button) and not rows_shown and not shown(mix.save_button),
          (rows_shown, shown(mix.connecting)))
    env.shot(window, 'o11c-02-mix-connecting')
    mix.cancel_button.emit('clicked')
    env.settle(1.0)
    snapshot = env.controller.snapshot()
    check('C2 Cancel sends entry.cancel and the controller abandons the start',
          any(c[0] == 'entry.cancel' for c in env.commands(mark)) and (snapshot.get('entry') or {}).get('state') == 'cancelled',
          (env.commands(mark), snapshot.get('entry')))
    check('C3 the previous room is kept, still playing', (snapshot.get('active') or {}).get('name') == 'Northern quiet'
          and status() == {'music': 'Playing', 'atmosphere': 'Playing'}, (snapshot.get('active'), status()))
    time.sleep(env.start_delay)
    env.settle(1.0)
    check('C4 nothing starts afterwards', not any(c[0] == 'start_music' for c in env.backend.calls[-3:])
          and (env.controller.snapshot().get('active') or {}).get('name') == 'Northern quiet', env.backend.calls[-4:])
    check('C5 the card and the Mix are back to the room', window.pending is None and not shown(mix.connecting)
          and shown(mix.rows['music']), (window.pending, shown(mix.connecting)))
    env.shot(window, 'o11c-03-after-cancel')
    env.start_delay = 0.
    window.sheet.set_open(False)
    window.close()
    env.pump(.4)

    # ---- popover per-layer line -------------------------------------------------------------------
    import session_client
    env.service.execute('layer', {'layer': 'music', 'action': 'pause'})
    env.settle(.6)
    host = next_mini.PopoverHost(env.app, lambda action, args=None, *_: session_client.request(action, args))
    env.pump(1.2)
    player = host.window.player
    line = [b for b in player.layer_buttons.values() if shown(b)]
    names = [b.get_tooltip_text() for b in line]
    check('P1 the popover shows a per-layer line under the name: Music (paused) and Ambience (playing)',
          shown(player.layer_line) and len(line) == 2 and 'Music is paused' in names[0] and 'Ambience is playing' in names[1],
          names)
    icons = [b.get_child().get_last_child().get_icon_name() for b in line]
    check('P1b as "Music ▮▮ · Ambience ▶": each shows its layer\'s state',
          icons == ['media-playback-pause-symbolic', 'media-playback-start-symbolic'], icons)
    check('P2 the popover keeps one room Pause/Play (Pause, something is audible)',
          shown(player.play) and player.play.get_tooltip_text() == 'Pause the room')
    env.shot(host.window, 'o11c-04-popover')
    mark = len(env.log)
    player.layer_buttons['music'].emit('clicked')
    env.settle(.8)
    check('P3 tapping Music plays music alone', status() == {'music': 'Playing', 'atmosphere': 'Playing'}
          and [c for c in env.commands(mark) if c[0] != 'inspect'][:1] == [('layer', {'layer': 'music', 'action': 'play'})],
          env.commands(mark))
    host.window.close()
    env.pump(.4)
    mini = next_mini.MiniPlayer(host, extras=False)
    check('P4 the mini player has no per-layer line', not mini.layer_buttons)

passed = sum(1 for _, ok in checks if ok)
print('%d/%d checks passed' % (passed, len(checks)))
