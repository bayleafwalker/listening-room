"""Evidence for the O9 UX leftovers on Now and in Preferences (WP2-WINDOW criterion 4).

- A start that fails (and one still connecting) in short and tall windows: the card never covers the
  "Connecting" chip or the room's name.
- A room named after the station that plays: Now says the name once.
- Undo of a default placement change says it is an undo.
Real GTK on a private Xvfb and D-Bus with the o6 fixtures; nothing reaches the network or the desktop.
Writes screenshots and ux.json to --output; exits non-zero when a check fails.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-wp2w-ux')
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:400]})
    print('%s %s %s' % ('PASS' if ok else 'FAIL', name, '' if ok else detail))


def bounds(widget, window):
    if widget is None or not widget.get_mapped() or not widget.get_visible():
        return None
    ok, rect = widget.compute_bounds(window)
    return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None


def overlaps(a, b):
    return bool(a and b) and a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def main():
    with ENV as env:
        env.service.execute('station.save', {'station': o6_env.RADIO_STATION})
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        import session_client
        from unittest import mock
        answer = session_client.request
        for height in (800, 560, 460):
            window = env.launch(width=1000, height=height, page='rooms')
            for case, state, delay in (('connecting', 'connecting', 6.0), ('slow', 'failed', 0.), ('refused', 'failed', 0.)):
                env.start_delay = delay
                env.next_window.START_GRACE_S = 1.0 if case == 'slow' else 8
                refuse = None
                if case != 'connecting':
                    def request(action, *a, case=case, **k):
                        if action == 'station.start' and case == 'refused':   # the controller says no
                            raise ValueError('fixture: the stream did not answer')
                        if action == 'station.start':                         # the answer comes late
                            time.sleep(2.5)
                        return answer(action, *a, **k)
                    refuse = mock.patch.object(session_client, 'request', side_effect=request)
                    refuse.start()
                window.start('station.start', {'id': station['id']}, 'Morning lift')
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and (window.pending or {}).get('state') != state:
                    env.pump(.1)
                env.pump(.6)
                card, chip, name = (bounds(w, window) for w in (window.pending_card, window.chip, window.room_name))
                if refuse is not None:
                    refuse.stop()
                check('%s card at %d px high is shown' % (case, height), card is not None, card)
                check('%s card at %d px high does not cover the chip' % (case, height), not overlaps(card, chip),
                      (card, chip, window.chip.get_text()))
                check('%s card at %d px high does not cover the room name' % (case, height), not overlaps(card, name),
                      (card, name))
                if state == 'failed':
                    check('a %s start with no room leaves no "Connecting" chip (%d px)' % (case, height),
                          not (chip and window.chip.get_text() == 'Connecting'), window.chip.get_text())
                env.shot(window, 'ux-%s-%d' % (case, height))
                window.dismiss_pending()
                env.start_delay = 0.
                env.settle(1.0)
            window.close()
            env.pump(.4)

        # A room named after what plays: the station is called what the stream calls itself.
        named = dict(o6_env.RADIO_STATION, name='SomaFM Groove Salad')
        env.service.execute('station.save', {'station': named})
        same = next(s for s in env.controller.stations() if s['name'] == 'SomaFM Groove Salad')
        env.service.execute('station.start', {'id': same['id']})
        env.settle(1.2)
        window = env.launch(width=1200, height=800, page=None)
        window.show_now()
        env.pump(1.0)
        shown = [window.room_name.get_text(), window.now_line.get_text(),
                 window.now_detail.get_text() if window.now_detail.get_visible() else '']
        name = shown[0]
        repeats = sum(1 for text in shown if name and name.casefold() in text.casefold())
        check('Now says the room name once (%r)' % (shown,), repeats == 1, shown)
        env.shot(window, 'ux-named-after-station')

        # Undo of a default placement change says it is an undo.
        window.open_preferences()
        env.pump(.8)
        prefs = window.preferences
        mark = len(env.log)
        prefs.placement_row.set_selected(2)
        env.settle(.8)
        toasts = []
        real = prefs.toast
        prefs.toast = lambda title, label=None, undo=None: (toasts.append((title, label, undo)), real(title, label, undo))
        prefs.placement_row.set_selected(3)
        env.settle(.8)
        undo = next((u for _t, label, u in toasts if label == 'Undo' and u), None)
        check('a placement change offers Undo', undo is not None, toasts)
        if undo:
            undo()
            env.settle(.8)
        sent = [args for action, args in env.commands(mark) if action == 'settings']
        check('the Undo is sent as an undo of the previous placement',
              bool(sent) and sent[-1]['values'] == {'placement': 'pin', 'undo': True}, sent)
        check('the placement shown after Undo is the previous one', env.controller.settings.get('placement') == 'pin',
              env.controller.settings.get('placement'))
        prefs.close()
        env.service.execute('leave', {})
        env.settle(1.0)
        window.close()
        env.pump(.3)
        (env.output / 'ux.json').write_text(json.dumps(RESULTS, indent=2) + '\n')
    return 0 if all(item['ok'] for item in RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
