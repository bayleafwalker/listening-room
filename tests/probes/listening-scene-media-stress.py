"""Scene media stress: rapid room previews, entering, leaving and closing the window, many times.

Drives the real window on a private Xvfb (o6_env) through random room-card previews, preview
switches and closes, entering a room from a preview, leaving it, and finally closing the window.
Each preview with motion creates a Gtk.MediaFile (a GstPlay thread) and drops the previous one,
which is the path that segfaulted on GstPlay's thread (g_object_unref <- gst_play_main).

GStreamer must be reachable (GST_PLUGIN_SYSTEM_PATH_1_0 and a Gst typelib), or no loop is ever
created and the probe fails instead of passing vacuously. A crash shows as the process dying with
SIGSEGV (exit 139); run it many times (see the CHANGELOG entry for the runner used).

    listening-scene-media-stress.py --output DIR [--cycles N] [--seed S]
"""
import random
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-scene-media-stress',
                 extra_args={'--cycles': dict(type=int, default=60), '--seed': dict(type=int, default=None)})


def main():
    rng = random.Random(ENV.args.seed)
    with ENV as env:
        window = env.launch(page='rooms')
        rooms = window.rooms
        cards = list(rooms.card_for.values())
        assert cards, 'Rooms shows no room cards'
        motion = 0
        counts = dict(preview=0, close=0, enter=0, leave=0)
        rss, threads = [], []
        for cycle in range(ENV.args.cycles):
            if cycle in (10, ENV.args.cycles - 1):
                rss.append(status('VmRSS') // 1024)
                threads.append(status('Threads'))
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
        window.close()
        env.pump(.5)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print('scene media stress: %d cycles %r, %d previews with a loop, survived; RSS %s MB, threads %s (cycle 10/last), peak RSS %d MB'
          % (ENV.args.cycles, counts, motion, '/'.join(map(str, rss)), '/'.join(map(str, threads)), peak), flush=True)


def status(field):
    with open('/proc/self/status') as lines:
        return next(int(line.split()[1]) for line in lines if line.startswith(field + ':'))


main()
