"""Cold start: process start to usable Rooms, then to every poster shown, and how long art holds the GTK thread.

Real window on a private Xvfb (o6_env), fake backend and fixtures, the real scene assets
(LISTENING_HUB_ASSETS). One cold start per process: run it several times and on an idle host before
trusting a wall time, and record the load average it prints.

Marks, in ms (process start is the kernel's start time of this Python process):
  imports      process start -> window module imported
  rooms        window constructed -> Rooms painted with its cards mapped (art may still be placeholders)
  art          window constructed -> the hero and every card show a texture, painted
  process      process start -> art

Art holds: every call on the GTK thread into the art code (texture_for, show_texture, show_art, the
hand-over of a texture decoded off-thread, Scene.show_room) is timed from window construction until the
art mark, and summed per main-loop iteration: the GTK thread cannot draw a frame until an iteration
ends, so "max_art_ms" is the longest art time in one iteration (window construction counts as one).
--max-art-ms N fails the run when it is over N.

    listening-cold-start.py --output DIR [--max-art-ms 50] [--label NAME]
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

T_SCRIPT = time.monotonic()
with open('/proc/self/stat') as _stat:
    _start_ticks = int(_stat.read().rsplit(')', 1)[1].split()[19])
with open('/proc/uptime') as _uptime:
    PROCESS_AGE = float(_uptime.read().split()[0]) - _start_ticks / os.sysconf('SC_CLK_TCK')

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-cold-start',
                 extra_args={'--max-art-ms': dict(type=float, default=0.), '--label': dict(default='run')})
MAIN = threading.main_thread()
ART_CALLS = ('texture_for', 'show_texture', 'show_art', '_art_ready')


def since_process(now):
    return (now - T_SCRIPT + PROCESS_AGE) * 1000


def main():
    holds = []
    iterations = [0.]           # art ms per main-loop iteration; the last one is the current iteration
    recording = [True]

    def timed(owner, name):
        original = getattr(owner, name, None)
        if original is None:
            return None

        def wrapper(*args, **kwargs):
            if not recording[0] or threading.current_thread() is not MAIN:
                return original(*args, **kwargs)
            start = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                spent = (time.monotonic() - start) * 1000
                holds.append((name, spent))
                iterations[-1] += spent
        setattr(owner, name, wrapper)
        return original, wrapper

    with ENV as env:
        import next_widgets
        t_imported = time.monotonic()
        replaced = {}
        for name in ART_CALLS:
            pair = timed(next_widgets, name)
            if pair:
                replaced[pair[0]] = pair[1]
        timed(next_widgets.Scene, 'show_room')
        by_id = {id(original): wrapper for original, wrapper in replaced.items()}
        for module in list(sys.modules.values()):     # names imported with `from next_widgets import ...`
            if module is next_widgets or not getattr(module, '__name__', '').startswith('next_'):
                continue
            for attribute, value in list(vars(module).items()):
                if id(value) in by_id:
                    setattr(module, attribute, by_id[id(value)])
        context, Gtk = env.context, env.Gtk

        t0 = time.monotonic()
        window = env.next_window.NextWindow(env.app)
        window.set_default_size(1200, 800)
        window.present()

        def cards():
            out, child = [], window.rooms.cards.get_first_child()
            while child is not None:
                out.append(child)
                child = child.get_next_sibling()
            return out

        def card_art(child):
            art = child.get_child().get_first_child()
            return getattr(art, '_shown_texture', None)

        def until(condition, timeout=20.):
            deadline = time.monotonic() + timeout
            while not condition() and time.monotonic() < deadline:
                iterations.append(0.)
                context.iteration(True)
            return condition()

        def painted():
            clock = window.get_frame_clock()
            frames = []
            handler = clock.connect('after-paint', lambda *_: frames.append(time.monotonic()))
            window.queue_draw()
            until(lambda: frames, 3.)
            clock.disconnect(handler)
            return frames[0] if frames else time.monotonic()

        rooms_ok = until(lambda: window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms'
                         and cards() and cards()[0].get_mapped())
        t_rooms = painted()
        placeholders = sum(card_art(child) is None for child in cards())
        hero = window.rooms.hero_art
        art_ok = until(lambda: all(isinstance(card_art(child), env.Gdk.Texture) for child in cards())
                       and (not hero.get_mapped() or isinstance(getattr(hero, '_shown_texture', None), env.Gdk.Texture)))
        t_art = painted()
        recording[0] = False
        sizes = sorted({(card_art(child).get_width(), card_art(child).get_height()) for child in cards()
                        if card_art(child) is not None})
        window.close()
        env.pump(.3)

    worst = max(holds, key=lambda item: item[1]) if holds else ('-', 0.)
    held = max(iterations)
    marks = {'label': ENV.args.label, 'load': open('/proc/loadavg').read().split()[:3],
             'imports_ms': round(since_process(t_imported)), 'rooms_ms': round((t_rooms - t0) * 1000),
             'art_ms': round((t_art - t0) * 1000), 'process_ms': round(since_process(t_art)),
             'cards': len(cards()), 'placeholders_at_rooms': placeholders, 'card_texture_sizes': sizes,
             'art_calls': len(holds), 'art_total_ms': round(sum(ms for _, ms in holds), 1),
             'max_art_ms': round(held, 1), 'longest_art_call_ms': round(worst[1], 1), 'longest_art_call': worst[0],
             'iterations_over_50ms': [round(ms, 1) for ms in iterations if ms > 50]}
    (ENV.output / ('cold-start-%s.json' % ENV.args.label)).write_text(json.dumps(marks, indent=1))
    print('cold start %s' % json.dumps(marks), flush=True)
    assert rooms_ok, 'Rooms never showed its cards'
    assert art_ok, 'some card or the hero never showed its poster'
    if ENV.args.max_art_ms:
        assert held <= ENV.args.max_art_ms, 'art held the GTK thread %.1f ms in one iteration, over %.0f ms' % (
            held, ENV.args.max_art_ms)


main()
