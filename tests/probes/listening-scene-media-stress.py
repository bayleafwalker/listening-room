"""Scene media stress: rapid room previews, entering, leaving and closing the window, many times.

Drives the real window on a private Xvfb (o6_env) through random room-card previews, preview
switches and closes, entering a room from a preview, leaving it, and finally closing the window.
Each preview with motion shows a scene loop (a Gtk.MediaFile and its GstPlay pipeline) and drops the
previous one; tearing a busy one down segfaulted on GstPlay's thread (g_object_unref <- gst_play_main)
and every pipeline ever created left GL threads behind.

GStreamer must be reachable (the dev shell has the package's plugins), or no loop is ever
created and the probe fails instead of passing vacuously. A crash shows as the process dying with
SIGSEGV (exit 139); run it many times.

--mode random   the mix above (default)
--mode preview  only preview open/close across rooms, for leak measurement

Takes a "base" sample on Rooms before any loop exists, previews every room once (warm-up: each loop
exists from then on), takes a "warm" sample, then runs the cycles. Prints a "sample" line every 10 cycles and after settling: RSS, threads,
GL threads (gldisplay-event + gstglcontext), open fds, and the live Scene and Gtk.MediaFile
wrappers after gc.collect(). With --flat, fails unless every later sample is within a small bound
of the warm one (no per-preview growth). With --budget-mb B, fails unless every sample's RSS is
within B of the base one: what all the loops cost together, held for the whole run (the "clips"
sample is bounded by the loop limit instead).

--extra-clips N  after the cycles, shows N more distinct loop files (links to a catalogue loop, as a
                 user's own clips would be) in one scene, then samples again ("clips"): the loops
                 beyond next_widgets.LOOP_LIMIT must show their still and add nothing.

--resume         after the cycles, waits until every loop has been released (stopped while nobody wants
                 it), then previews each room again: the loop must move again, the picture must never be
                 left with nothing to draw (the scene's paintable always has an image), and the GTK thread
                 must not be held over 100 ms while it resumes.

--release-ms MS  release unwanted loops after MS instead of next_widgets.RELEASE_MS: a short delay stops
                 and restarts pipelines between nearly every action, prerolls included.

--seam           after the cycles, enters a room and watches its scene past the end of the loop:
                 the timestamp must wrap back to the start (Gtk.MediaFile's own looping stops at the
                 end here), on the same MediaFile, with no GTK-thread gap over 100 ms around it.

    listening-scene-media-stress.py --output DIR [--cycles N] [--seed S] [--mode M] [--flat]
                                    [--budget-mb B] [--extra-clips N] [--seam] [--resume] [--release-ms MS]
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
                             '--flat': dict(action='store_true'), '--budget-mb': dict(type=int, default=0),
                             '--extra-clips': dict(type=int, default=0), '--seam': dict(action='store_true'),
                             '--resume': dict(action='store_true'), '--release-ms': dict(type=int, default=0)})


RELEASES = [0]


def main():
    global next_widgets
    rng = random.Random(ENV.args.seed)
    with ENV as env:
        import next_widgets
        if ENV.args.release_ms:
            next_widgets.RELEASE_MS = ENV.args.release_ms
        stop = next_widgets._Loop._release

        def counted(loop):
            RELEASES[0] += not loop.wanted and not loop.released
            return stop(loop)
        next_widgets._Loop._release = staticmethod(counted)
        window = env.launch(page='rooms')
        rooms = window.rooms
        cards = list(rooms.card_for.values())
        assert cards, 'Rooms shows no room cards'
        motion = 0
        counts = dict(preview=0, close=0, enter=0, leave=0)
        env.pump(2)
        samples = {'base': sample(env, 'base')}
        for card in cards:                # warm-up: show every loop once
            rooms.open_preview(card)
            env.pump(.6)
            motion += bool(rooms.preview_widgets['scene'].want_motion)
            rooms.close_preview(animate=False)
        env.pump(3)
        samples['warm'] = sample(env, 'warm')
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
        env.pump(3)
        samples['settled'] = sample(env, 'settled')
        if ENV.args.resume:
            samples['released'] = resume(env, window, rooms, cards)
        if ENV.args.seam:
            seam(env, window, rooms, cards)
        if ENV.args.extra_clips:
            samples['clips'] = extra_clips(env, ENV.args.extra_clips)
        window.close()
        env.pump(.5)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print('scene media stress: %d cycles %r, %d previews with a loop, %d loop releases, survived, peak RSS %d MB'
          % (ENV.args.cycles, counts, motion, RELEASES[0], peak), flush=True)
    if ENV.args.budget_mb:
        over = {label: values['rss_mb'] - samples['base']['rss_mb'] for label, values in samples.items()
                if label != 'clips' and values['rss_mb'] - samples['base']['rss_mb'] > ENV.args.budget_mb}
        assert not over, 'loops cost more than %d MB over the base sample: %r' % (ENV.args.budget_mb, over)
        print('budget: every sample within %d MB of the base sample (warm +%d MB)'
              % (ENV.args.budget_mb, samples['warm']['rss_mb'] - samples['base']['rss_mb']), flush=True)
    if ENV.args.flat:
        base = samples['warm']
        bounds = {'rss_mb': 60, 'threads': 8, 'gl_threads': 0, 'fds': 8, 'scenes': 2, 'media': 0}
        # Mid-run, a closing preview page lives until its removal timeout (up to 260 ms), so live
        # Scenes are only counted once settled; every other measure is checked at every sample.
        grown = {(label, key): (base[key], values[key]) for label, values in samples.items() if label not in ('base', 'clips')
                 for key, limit in bounds.items()
                 if values[key] - base[key] > limit and (key != 'scenes' or label == 'settled')}
        assert not grown, 'grew beyond %r of the warm sample: %r' % (bounds, grown)
        print('flat: every sample within %r of the warm sample' % bounds, flush=True)


def seam(env, window, rooms, cards):
    """Enter a room and watch its loop pass its end: it must start over, without a GTK-thread stall."""
    import time
    window.show_rooms()
    rooms.open_preview(cards[0])
    env.pump(.5)
    rooms.enter(rooms.opened)
    env.pump(2)
    scene = window.scene
    media = scene.media
    assert media is not None and scene.playing_motion, 'the entered room plays no loop'
    duration = media.get_duration() / 1e6
    ticks, wraps, last = [], 0, media.get_timestamp()
    deadline = time.monotonic() + duration + 6
    while time.monotonic() < deadline:
        env.context.iteration(False)
        ticks.append(time.monotonic())
        now = media.get_timestamp()
        wraps += now + 1e6 < last
        last = now
        time.sleep(.005)
    gap = max(b - a for a, b in zip(ticks, ticks[1:])) * 1000
    print('seam: loop %.1f s, wrapped %d times, still the same MediaFile %s, playing %s, worst GTK-thread gap %.0f ms'
          % (duration, wraps, scene.media is media, scene.playing_motion, gap), flush=True)
    assert wraps >= 1, 'the loop stopped at its end instead of starting over'
    assert scene.media is media, 'the scene changed its MediaFile'
    assert gap < 100, 'the GTK thread stalled %.0f ms around the seam' % gap
    window.leave_room()
    env.pump(1)


def resume(env, window, rooms, cards):
    """Every loop released, then each room previewed again: motion returns, never a blank picture, no stall."""
    import time
    window.show_rooms()
    loops = next_widgets._LOOPS.values()
    deadline = time.monotonic() + 10
    while not all(loop.released for loop in loops) and time.monotonic() < deadline:
        env.pump(.2)
    env.pump(2)
    released = sample(env, 'released')
    print('resume: %d of %d loops released' % (sum(loop.released for loop in loops), len(loops)), flush=True)
    assert loops and all(loop.released for loop in loops), 'loops still hold their pipelines off screen'
    worst_gap, blanks, starts = 0, 0, []
    for card in cards:
        rooms.open_preview(card)
        scene = rooms.preview_widgets['scene']
        start = last = time.monotonic()
        moving = None
        while time.monotonic() - start < 3:
            env.context.iteration(False)
            now = time.monotonic()
            worst_gap, last = max(worst_gap, now - last), now
            paintable = scene.picture.paintable
            blanks += paintable is not None and not paintable.get_intrinsic_width()
            if moving is None and scene.media is not None and scene.playing_motion and scene.media.get_timestamp() > 0:
                moving = now - start
            time.sleep(.002)
        starts.append(moving)
        rooms.close_preview(animate=False)
        env.pump(.3)
    print('resume: motion after %s s, %d blank picture iterations, worst GTK-thread gap %.0f ms'
          % ([None if m is None else round(m, 2) for m in starts], blanks, worst_gap * 1000), flush=True)
    assert all(m is not None for m in starts), 'a released loop did not move again'
    assert blanks == 0, 'the scene had nothing to draw while its loop resumed'
    assert worst_gap < .1, 'the GTK thread stalled %.0f ms while a loop resumed' % (worst_gap * 1000)
    return released


def extra_clips(env, count):
    """Show ``count`` more distinct loop files, as a user's own clips would be, and sample after."""
    import tempfile
    loops = sorted(Path(os.environ['LISTENING_HUB_ASSETS']).glob('room-*.mp4'))
    assert loops, 'no catalogue loops to link'
    folder = Path(tempfile.mkdtemp(prefix='clips-', dir=os.environ['HOME']))
    scene = next_widgets.Scene()
    holder = env.Gtk.Window(child=scene, default_width=480, default_height=270)
    holder.present()
    shown = 0
    for index in range(count):
        clip = folder / ('clip-%d.mp4' % index)
        clip.symlink_to(loops[index % len(loops)])
        scene.show_room({'still': None, 'poster': None, 'loop': str(clip)}, True)
        shown += scene.media is not None
        env.pump(.3)
    scene.close()
    env.pump(2)
    pool = len(next_widgets._LOOPS)
    print('extra clips: %d shown, %d played a loop, %d loops in the pool (limit %d)'
          % (count, shown, pool, next_widgets.LOOP_LIMIT), flush=True)
    assert pool <= next_widgets.LOOP_LIMIT, 'the loop pool grew past its limit'
    values = sample(env, 'clips')
    holder.destroy()
    return values


def sample(env, label):
    gc.collect()
    live = gc.get_objects()
    names = []
    for task in os.listdir('/proc/self/task'):
        try:
            with open('/proc/self/task/%s/comm' % task) as comm:
                names.append(comm.read().strip())
        except OSError:
            pass
    values = {'rss_mb': status('VmRSS') // 1024, 'threads': status('Threads'),
              'gl_threads': sum(name in ('gldisplay-event', 'gstglcontext') for name in names),
              'fds': len(os.listdir('/proc/self/fd')),
              'scenes': sum(isinstance(o, next_widgets.Scene) for o in live),
              'media': sum(isinstance(o, env.Gtk.MediaFile) for o in live)}
    print('sample %s %s' % (label, ' '.join('%s=%s' % item for item in values.items())), flush=True)
    return values


def status(field):
    with open('/proc/self/status') as lines:
        return next(int(line.split()[1]) for line in lines if line.startswith(field + ':'))


main()
