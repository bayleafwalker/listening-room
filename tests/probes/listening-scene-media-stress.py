"""Scene media stress: rapid room previews, entering, leaving and closing the window, many times.

Drives the real window on a private Xvfb (o6_env) through random room-card previews, preview
switches and closes, entering a room from a preview, leaving it, and finally closing the window.
Each preview with motion creates a Gtk.MediaFile (a GstPlay thread) and drops the previous one,
which is the path that segfaulted on GstPlay's thread (g_object_unref <- gst_play_main).

GStreamer must be reachable (GST_PLUGIN_SYSTEM_PATH_1_0 and a Gst typelib), or no loop is ever
created and the probe fails instead of passing vacuously. A crash shows as the process dying with
SIGSEGV (exit 139); run it many times.

--mode random   the mix above (default)
--mode preview  only preview open/close across rooms, for leak measurement

Prints one "sample" line every 10 cycles and after settling: RSS, threads, open fds, and the live
Scene and Gtk.MediaFile wrappers after gc.collect(). With --flat, fails unless the settled sample
is within a small bound of the cycle-10 sample (no per-preview growth).

    listening-scene-media-stress.py --output DIR [--cycles N] [--seed S] [--mode M] [--flat]
"""
import gc
import os
import random
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-scene-media-stress',
                 extra_args={'--cycles': dict(type=int, default=60), '--seed': dict(type=int, default=None),
                             '--mode': dict(default='random', choices=['random', 'preview']),
                             '--flat': dict(action='store_true')})


def main():
    global next_widgets
    rng = random.Random(ENV.args.seed)
    with ENV as env:
        import next_widgets
        window = env.launch(page='rooms')
        rooms = window.rooms
        cards = list(rooms.card_for.values())
        assert cards, 'Rooms shows no room cards'
        motion = 0
        counts = dict(preview=0, close=0, enter=0, leave=0)
        samples = {}
        for cycle in range(ENV.args.cycles):
            if cycle and cycle % 10 == 0:
                samples[cycle] = sample(env, cycle)
            if ENV.args.mode == 'preview':
                action = 'close' if rooms.opened is not None else 'preview'
            else:
                action = rng.choices(['preview', 'close', 'enter', 'leave'], [6, 2, 2, 1])[0]
            if action in ('close', 'enter') and rooms.opened is None:
                action = 'preview'
            if action == 'preview':
                window.show_rooms()
                rooms.open_preview(rng.choice(cards))
                motion += bool(rooms.preview_widgets['scene'].want_motion)
            elif action == 'close':
                rooms.close_preview(animate=rng.random() < .5)
            elif action == 'enter':
                rooms.enter(rooms.opened)
            else:
                window.leave_room()
            counts[action] += 1
            env.pump(rng.choice([0, 0, .005, .02, .05, .15, .4]))
        assert motion, 'no preview ever created a scene loop: is GStreamer reachable?'
        rooms.close_preview(animate=False)
        env.pump(8)                       # hidden loops go once quiet; their players a few seconds later
        settled = sample(env, 'settled')
        window.close()
        env.pump(.5)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print('scene media stress: %d cycles %r, %d previews with a loop, survived, peak RSS %d MB'
          % (ENV.args.cycles, counts, motion, peak), flush=True)
    if ENV.args.flat and 10 in samples:
        base = samples[10]
        bounds = {'rss_mb': 150, 'threads': 40, 'fds': 40, 'scenes': 2, 'media': 2}
        grown = {key: (base[key], settled[key]) for key, limit in bounds.items() if settled[key] - base[key] > limit}
        assert not grown, 'grew from cycle 10 to settled beyond %r: %r' % (bounds, grown)
        print('flat: settled within %r of cycle 10' % bounds, flush=True)


def sample(env, label):
    gc.collect()
    live = gc.get_objects()
    values = {'rss_mb': status('VmRSS') // 1024, 'threads': status('Threads'), 'fds': len(os.listdir('/proc/self/fd')),
              'scenes': sum(isinstance(o, next_widgets.Scene) for o in live),
              'media': sum(isinstance(o, env.Gtk.MediaFile) for o in live)}
    print('sample %s %s' % (label, ' '.join('%s=%s' % item for item in values.items())), flush=True)
    return values


def status(field):
    with open('/proc/self/status') as lines:
        return next(int(line.split()[1]) for line in lines if line.startswith(field + ':'))


main()
