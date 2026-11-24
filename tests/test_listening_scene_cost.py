"""What art and scene loops cost the window: posters decode off the GTK thread at card size, the loop
pool has a limit, and libav decoders run one thread instead of one per core."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_widgets
import poster_cards
from test_listening_render_cache import FakeMedia, FakePicture

from gi.repository import GLib


def pump(condition, timeout=5.):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(.005)
    return condition()


def poster(path, size=(1440, 810), colour=(40, 90, 160)):
    from PIL import Image
    Image.new('RGB', size, colour).save(path)
    return str(path)


class ArtOffTheGtkThread(unittest.TestCase):
    def setUp(self):
        next_widgets._TEXTURES.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(next_widgets._TEXTURES.clear)
        self.loaded = []

    def fake_loader(self):
        def load(path):
            self.loaded.append((path, threading.current_thread().name))
            return object()
        patch = mock.patch.object(next_widgets.Gdk.Texture, 'new_from_filename', side_effect=load)
        patch.start()
        self.addCleanup(patch.stop)

    def file(self, name):
        path = Path(self.dir.name) / name
        path.write_bytes(b'art')
        return str(path)

    def test_new_art_decodes_on_a_worker_and_is_handed_over_on_the_main_loop(self):
        self.fake_loader()
        picture = FakePicture()
        next_widgets.show_art(picture, self.file('poster.png'))
        self.assertIsNone(picture.paintable, 'nothing is decoded on the GTK thread: the picture waits')
        self.assertTrue(pump(lambda: picture.paintable is not None))
        self.assertEqual(len(self.loaded), 1)
        self.assertTrue(self.loaded[0][1].startswith('art'), self.loaded[0])

    def test_art_decoded_before_shows_at_once(self):
        self.fake_loader()
        path = self.file('poster.png')
        first = FakePicture()
        next_widgets.show_art(first, path)
        pump(lambda: first.paintable is not None)
        again = FakePicture()
        next_widgets.show_art(again, path)
        self.assertIs(again.paintable, first.paintable)
        self.assertEqual(len(self.loaded), 1)

    def test_pictures_waiting_for_the_same_art_share_one_decode(self):
        self.fake_loader()
        path = self.file('poster.png')
        pictures = [FakePicture() for _ in range(5)]
        for picture in pictures:
            next_widgets.show_art(picture, path)
        self.assertTrue(pump(lambda: all(p.paintable is not None for p in pictures)))
        self.assertEqual(len(self.loaded), 1)

    def test_the_latest_request_for_a_picture_wins(self):
        self.fake_loader()
        shown = self.file('shown.png')
        holder = FakePicture()
        next_widgets.show_art(holder, shown)
        pump(lambda: holder.paintable is not None)
        picture = FakePicture()
        next_widgets.show_art(picture, self.file('slow.png'))      # still decoding...
        next_widgets.show_art(picture, shown)                      # ...when the room changes again
        pump(lambda: len(self.loaded) == 2 and not next_widgets._ART['waiting'])
        self.assertIs(picture.paintable, holder.paintable)

    def test_card_size_uses_the_pre_scaled_card_file(self):
        self.fake_loader()
        path = self.file('room.png')
        card = self.file('room.card.jpg')
        picture = FakePicture()
        next_widgets.show_art(picture, path, next_widgets.CARD_SIZE)
        pump(lambda: picture.paintable is not None)
        self.assertEqual([item[0] for item in self.loaded], [card])

    def test_without_a_card_file_the_poster_is_scaled_on_the_worker(self):
        path = poster(Path(self.dir.name) / 'yours.png')
        picture = FakePicture()
        next_widgets.show_art(picture, path, next_widgets.CARD_SIZE)
        self.assertTrue(pump(lambda: picture.paintable is not None))
        texture = picture.paintable
        self.assertIsInstance(texture, next_widgets.Gdk.Texture)
        self.assertEqual((texture.get_width(), texture.get_height()), next_widgets.CARD_SIZE)

    def test_unreadable_or_missing_art_shows_nothing(self):
        picture = FakePicture()
        next_widgets.show_art(picture, self.file('broken.png'))
        self.assertTrue(pump(lambda: not next_widgets._ART['waiting']))
        self.assertIsNone(picture.paintable)
        next_widgets.show_art(picture, os.path.join(self.dir.name, 'gone.png'))
        next_widgets.show_art(picture, None)
        self.assertIsNone(picture.paintable)


class PosterCards(unittest.TestCase):
    def test_cover_keeps_the_aspect_and_never_upscales(self):
        self.assertEqual(poster_cards.cover(1440, 810), (640, 360))
        self.assertEqual(poster_cards.cover(810, 1440), (640, 1138))
        self.assertEqual(poster_cards.cover(320, 180), (320, 180))

    def test_the_build_writes_a_card_for_every_poster(self):
        with tempfile.TemporaryDirectory() as folder:
            poster(Path(folder) / 'room-a.png')
            poster(Path(folder) / 'room-b.png', (768, 432))
            poster(Path(folder) / 'room-a-still.jpg')
            self.assertEqual(poster_cards.write_cards(folder), 2)
            from PIL import Image
            with Image.open(Path(folder) / 'room-a.card.jpg') as card:
                self.assertEqual(card.size, poster_cards.CARD_SIZE)
            self.assertEqual(poster_cards.card_file(str(Path(folder) / 'room-b.png')), str(Path(folder) / 'room-b.card.jpg'))
            self.assertIsNone(poster_cards.card_file(str(Path(folder) / 'room-a-still.jpg')))
            self.assertIsNone(poster_cards.card_file(None))


class LoopPoolLimit(unittest.TestCase):
    """Loops are never torn down, so the pool itself must be bounded: past LOOP_LIMIT files a scene
    shows its still, and the loops already made keep playing."""
    def setUp(self):
        self.created = []

        def new(path):
            self.created.append(FakeMedia(path))
            return self.created[-1]

        for item in [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                     mock.patch.object(next_widgets, 'gst_ready', return_value=True),
                     mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                     mock.patch.dict(next_widgets._LOOPS, clear=True)]:
            item.start()
            self.addCleanup(item.stop)

    def scene(self):
        scene = next_widgets.Scene.__new__(next_widgets.Scene)
        scene.picture = FakePicture()
        scene.media = scene.loop = scene.still = scene.key = None
        scene.error_handler, scene.want_motion = 0, False
        scene.mapped, scene.minimized = True, False
        scene.get_mapped = lambda: True
        return scene

    def test_a_preview_attaches_its_loop_only_after_the_double_click_time(self):
        preview = self.scene()
        preview.motion_delay_ms = 450
        preview.show_room({'still': 'lake.jpg', 'loop': 'lake.mp4'}, True)
        self.assertEqual((self.created, preview.picture.paintable), ([], 'still:lake.jpg'))
        self.assertTrue(preview.delayed)
        next_widgets.Scene._motion_due(preview, 'lake.mp4')
        self.assertTrue(preview.playing_motion)
        skimmed = self.scene()
        skimmed.motion_delay_ms = 450
        skimmed.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        skimmed.close()                           # the next card, or a double-click that enters
        self.assertFalse(skimmed.delayed)
        self.assertEqual([media.path for media in self.created], ['lake.mp4'], 'a skimmed card made no pipeline')

    def test_clips_beyond_the_limit_show_their_still(self):
        scene = self.scene()
        limit = next_widgets.LOOP_LIMIT
        for index in range(limit + 3):
            scene.show_room({'still': 'clip-%d.jpg' % index, 'loop': 'clip-%d.mp4' % index}, True)
        self.assertEqual(len(self.created), limit)
        self.assertFalse(scene.want_motion)
        self.assertIsNone(scene.media)
        self.assertEqual(scene.picture.paintable, 'still:clip-%d.jpg' % (limit + 2))
        scene.show_room({'still': 'clip-0.jpg', 'loop': 'clip-0.mp4'}, True)     # one already made still plays
        self.assertTrue(scene.playing_motion)
        self.assertEqual(len(self.created), limit)
        for media in self.created:
            self.assertNotIn('clear', media.calls)


class LoopsRestartAtTheEnd(unittest.TestCase):
    """Gtk.MediaFile's set_loop(True) leaves the stream stopped at its end (timestamp == duration, playing,
    not ended): a playing loop seeks back to the start on the same MediaFile, never a new one."""
    class Media(FakeMedia):
        def __init__(self, path=None):
            super().__init__(path)
            self.timestamp, self.duration, self.seeking, self.seeks = 0, 20_000_000, False, []

        def get_timestamp(self):
            return self.timestamp

        def get_duration(self):
            return self.duration

        def is_seeking(self):
            return self.seeking

        def seek(self, position):
            self.seeks.append(position)
            self.timestamp = position

        def tick(self, timestamp):
            self.timestamp = timestamp
            for name, callback, data in list(self.handlers.values()):
                if name == 'notify::timestamp':
                    callback(self, None, *data)

    def setUp(self):
        self.created = []

        def new(path):
            self.created.append(self.Media(path))
            return self.created[-1]

        for item in [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                     mock.patch.object(next_widgets, 'gst_ready', return_value=True),
                     mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                     mock.patch.dict(next_widgets._LOOPS, clear=True)]:
            item.start()
            self.addCleanup(item.stop)

    def test_a_playing_loop_seeks_to_the_start_at_its_end(self):
        scene = LoopPoolLimit.scene(self)
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        media = self.created[0]
        media.tick(19_960_000)
        self.assertEqual(media.seeks, [], 'not before the end')
        media.tick(20_000_000)
        self.assertEqual(media.seeks, [0])
        self.assertTrue(scene.playing_motion)
        media.seeking = True
        media.tick(20_000_000)
        self.assertEqual(media.seeks, [0], 'one seek at a time')
        self.assertEqual(len(self.created), 1)
        self.assertNotIn('clear', media.calls)

    def test_a_paused_loop_holds_no_watch(self):
        scene = LoopPoolLimit.scene(self)
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        scene.close()
        self.assertEqual(self.created[0].handlers, {})


class PausedLoopsRelease(unittest.TestCase):
    """A loop nobody wants for RELEASE_MS is stopped through its GstPlay (threads and decoder go) and the
    heap is trimmed; the MediaFile is never cleared or replaced, and wanting it again plays it again."""
    def setUp(self):
        self.created, self.stopped, self.trims = [], [], []

        def new(path):
            self.created.append(FakeMedia(path))
            return self.created[-1]

        for item in [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                     mock.patch.object(next_widgets, 'gst_ready', return_value=True),
                     mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                     mock.patch.object(next_widgets, 'gst_player', side_effect=lambda media: 'player:%s' % media.path),
                     mock.patch.object(next_widgets, 'RELEASE_MS', 30),
                     mock.patch.object(next_widgets, 'TRIM_MS', 10),
                     mock.patch.object(next_widgets, '_sweep', side_effect=self.trim),
                     mock.patch.dict(next_widgets._PLAY, {'stop': self.stopped.append, 'sources': [], 'stopping': [],
                                                          'source': lambda player: ('source', player)}, clear=True),
                     mock.patch.dict(next_widgets._LOOPS, clear=True)]:
            item.start()
            self.addCleanup(item.stop)

    def trim(self):
        self.trims.append(time.monotonic())
        next_widgets._PLAY['sweep'] = 0
        return False

    def test_an_unwanted_loop_is_stopped_after_the_delay_and_plays_again_when_wanted(self):
        scene = LoopPoolLimit.scene(self)
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        loop, media = next_widgets._LOOPS['drive.mp4'], self.created[0]
        scene.close()
        self.assertEqual(self.stopped, [], 'not at once: leaving a preview to enter its room keeps the loop')
        self.assertTrue(pump(lambda: self.stopped and self.trims, 2))
        self.assertEqual(self.stopped, ['player:drive.mp4'])
        self.assertEqual(next_widgets._PLAY['sources'], [('source', 'player:drive.mp4')], 'its file is closed after')
        self.assertTrue(loop.released)
        scene = LoopPoolLimit.scene(self)               # the next preview of the room
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        self.assertTrue(scene.playing_motion)
        self.assertFalse(loop.released)
        self.assertEqual(len(self.created), 1, 'the same MediaFile plays again')
        self.assertNotIn('clear', media.calls)

    def test_wanted_again_within_the_delay_it_is_never_stopped(self):
        preview = LoopPoolLimit.scene(self)
        preview.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        preview.close()
        now = LoopPoolLimit.scene(self)               # Enter: the room's scene takes the loop over
        now.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        pump(lambda: False, .15)
        self.assertEqual(self.stopped, [])
        self.assertFalse(next_widgets._LOOPS['drive.mp4'].releasing)

    def test_a_released_loop_is_stopped_once(self):
        scene = LoopPoolLimit.scene(self)
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        scene._visibility(mapped=False)
        self.assertTrue(pump(lambda: self.stopped, 2))
        scene._visibility(minimized=True)
        scene.close()
        pump(lambda: False, .15)
        self.assertEqual(self.stopped, ['player:drive.mp4'])

    def test_without_its_player_a_loop_only_pauses(self):
        next_widgets.gst_player.side_effect = lambda media: None
        scene = LoopPoolLimit.scene(self)
        scene.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        scene.close()
        self.assertFalse(next_widgets._LOOPS['drive.mp4'].releasing)
        self.assertEqual(self.created[0].calls[-1], 'pause')


class OldSourcesAreClosed(unittest.TestCase):
    """A stopped pipeline's old source keeps its file open (GTK 4.24 / GStreamer 1.28 hold it): the sweep
    after a release closes each one once it is out of its pipeline, retries while it is not, and lets go
    of it unclosed after a bounded number of tries; then it trims the heap."""
    def setUp(self):
        self.closed, self.dead, self.trims = [], set(), []

        def close(source, give_up=False):
            if source in self.dead or give_up:
                self.closed.append((source, source in self.dead))
                return True
            return False

        self.ready = set()
        for item in [mock.patch.dict(next_widgets._PLAY, {'close': close, 'sources': [], 'tries': 0, 'stopping': [],
                                                          'stopped': lambda player: player in self.ready}, clear=True),
                     mock.patch.object(next_widgets, 'TRIM_MS', 5),
                     mock.patch.object(next_widgets.threading, 'Thread',
                                       side_effect=lambda **kw: mock.Mock(start=lambda: self.trims.append(kw)))]:
            item.start()
            self.addCleanup(item.stop)

    def test_dead_sources_close_at_once_and_live_ones_are_retried_then_dropped(self):
        self.dead.add('old')
        next_widgets._PLAY['sources'] = ['old', 'busy']
        next_widgets._sweep()
        self.assertEqual(self.closed, [('old', True)])
        self.assertEqual(next_widgets._PLAY['sources'], ['busy'])
        self.assertEqual(self.trims, [], 'trimmed once the sweep is done')
        self.assertTrue(pump(lambda: self.trims, 3))
        self.assertEqual(self.closed, [('old', True), ('busy', False)], 'let go of, never closed')
        self.assertEqual(next_widgets._PLAY['sources'], [])

    def test_the_heap_is_trimmed_once_every_stopped_pipeline_is_at_ready(self):
        drive, lake, park = (mock.Mock(player=name, released=True) for name in ('drive', 'lake', 'park'))
        park.released = False                      # wanted again before it stopped
        next_widgets._PLAY['stopping'] = [drive, lake, park]
        self.ready.add('drive')
        next_widgets._sweep()
        self.assertEqual((next_widgets._PLAY['stopping'], self.trims), ([lake], []))
        self.ready.add('lake')
        self.assertTrue(pump(lambda: self.trims, 1))
        self.assertEqual(next_widgets._PLAY['stopping'], [])


class StoppedLoopsDropTheirSink(unittest.TestCase):
    """Once a released loop's pipeline is at READY its video sink is dropped (once); wanted again, the
    player gets a new sink from the same renderer before it plays. The MediaFile is the same throughout."""
    def setUp(self):
        self.calls, self.ready = [], set()
        for item in [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=FakeMedia),
                     mock.patch.object(next_widgets, 'gst_player', side_effect=lambda media: 'player:%s' % media.path),
                     mock.patch.object(next_widgets, 'TRIM_MS', 5),
                     mock.patch.object(next_widgets.threading, 'Thread', side_effect=lambda **kw: mock.Mock()),
                     mock.patch.dict(next_widgets._PLAY, {
                         'close': lambda source, give_up=False: True, 'sources': [], 'tries': 0, 'stopping': [],
                         'stopped': lambda player: player in self.ready,
                         'shed': lambda player: self.calls.append(('shed', player)) or 'renderer:%s' % player,
                         'renew': lambda player, renderer: self.calls.append(('renew', player, renderer)),
                         'unref': lambda renderer: self.calls.append(('unref', renderer))}, clear=True),
                     mock.patch.object(next_widgets._Warm, 'start')]:
            item.start()
            self.addCleanup(item.stop)

    def released(self, path):
        loop = next_widgets._Loop(path)
        loop.released = True
        next_widgets._PLAY['stopping'].append(loop)
        return loop

    def test_the_sink_goes_once_the_pipeline_is_at_ready_and_comes_back_before_it_plays(self):
        loop = self.released('drive.mp4')
        next_widgets._sweep()
        self.assertEqual(self.calls, [], 'not before READY')
        self.ready.add('player:drive.mp4')
        next_widgets._sweep()
        self.assertEqual(self.calls, [('shed', 'player:drive.mp4')])
        self.assertTrue(loop.shed)
        self.assertEqual(loop.renderer, 'renderer:player:drive.mp4')
        media = loop.media
        media.play = lambda: self.calls.append(('play',))
        loop.want('scene', True)
        self.assertEqual(self.calls[1:], [('renew', 'player:drive.mp4', 'renderer:player:drive.mp4'), ('play',)])
        self.assertFalse(loop.shed)
        self.assertIs(loop.media, media)
        loop.want('scene', True)
        self.assertEqual(self.calls[-1], ('play',), 'renewed only once')
        self.assertEqual(len([c for c in self.calls if c[0] == 'renew']), 1)

    def test_a_second_release_keeps_the_one_renderer(self):
        loop = self.released('drive.mp4')
        self.ready.add('player:drive.mp4')
        next_widgets._sweep()
        loop.shed, loop.released = False, True
        next_widgets._PLAY['stopping'].append(loop)
        next_widgets._sweep()
        self.assertEqual(self.calls[-1], ('unref', 'renderer:player:drive.mp4'), 'the same paintable, already held')
        self.assertEqual(loop.renderer, 'renderer:player:drive.mp4')

    def test_nothing_is_dropped_for_a_loop_wanted_again_or_without_shedding(self):
        loop = self.released('drive.mp4')
        loop.released = False
        self.ready.add('player:drive.mp4')
        next_widgets._sweep()
        next_widgets._PLAY['shed'] = None
        self.released('lake.mp4')
        self.ready.add('player:lake.mp4')
        next_widgets._sweep()
        self.assertEqual(self.calls, [])


class DownloadSetUpWaitsForQuiet(unittest.TestCase):
    """GDK's dmabuf download set-up is paid once after the first shed, only after WARM_MS without input,
    repaint or a change to which loops play; it never starts twice."""
    def setUp(self):
        self.texture = mock.Mock(spec=next_widgets.Gdk.Texture)
        self.texture.get_width.return_value, self.texture.get_height.return_value = 4, 2
        self.media = mock.Mock(get_current_image=mock.Mock(return_value=self.texture))
        self.window, self.clock = mock.Mock(), mock.Mock()
        self.window.get_frame_clock.return_value = self.clock
        for item in [mock.patch.object(next_widgets._Warm, 'pending', None),
                     mock.patch.object(next_widgets._Warm, 'source', 0),
                     mock.patch.object(next_widgets._Warm, 'watches', []),
                     mock.patch.object(next_widgets._Warm, 'controllers', []),
                     mock.patch.object(next_widgets._Warm, 'quiet_since', 0),
                     mock.patch.object(next_widgets.Gtk.Window, 'list_toplevels', return_value=[self.window]),
                     mock.patch.object(next_widgets.Gtk, 'EventControllerLegacy'),
                     mock.patch.object(next_widgets.GLib, 'timeout_add', return_value=7),
                     mock.patch.dict(next_widgets._LOOPS, clear=True)]:
            item.start()
            self.addCleanup(item.stop)

    def age(self):
        next_widgets._Warm.quiet_since -= next_widgets.WARM_MS * 1000 + 1

    def test_it_waits_for_quiet_then_downloads_the_last_frame_once_and_unhooks(self):
        warm = next_widgets._Warm
        warm.start(self.media)
        self.window.add_controller.assert_called_once()
        self.clock.connect.assert_called_once_with('paint', warm.busy)
        self.assertTrue(warm.poll(), 'not before WARM_MS')
        self.age()
        next_widgets._LOOPS['drive.mp4'] = mock.Mock(wanted={'scene'})
        self.assertTrue(warm.poll(), 'not while a loop plays')
        next_widgets._LOOPS['drive.mp4'].wanted = set()
        self.age()
        warm.busy()                                  # any input, repaint or change of wanted loops
        self.assertTrue(warm.poll(), 'the wait starts over')
        self.texture.download.assert_not_called()
        self.age()
        self.assertFalse(warm.poll())
        args = self.texture.download.call_args[0]
        self.assertEqual((len(args[0]), args[1]), (4 * 2 * 4, 4 * 4))
        self.window.remove_controller.assert_called_once()
        self.clock.disconnect.assert_called_once()
        warm.start(self.media)
        self.assertEqual(self.window.add_controller.call_count, 1, 'once for the life of the process')

    def test_any_change_to_the_wanted_loops_counts_as_activity(self):
        with mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=FakeMedia), \
                mock.patch.object(next_widgets, 'gst_player', return_value=None):
            loop = next_widgets._Loop('drive.mp4')
            for playing in (True, False):
                next_widgets._Warm.quiet_since = 0
                loop.want('scene', playing)
                self.assertGreater(next_widgets._Warm.quiet_since, 0, 'wanted %s' % playing)

    def test_a_still_that_is_not_a_texture_is_left_alone(self):
        self.media.get_current_image.return_value = mock.Mock(spec=[])
        next_widgets._Warm.start(self.media)
        self.age()
        self.assertFalse(next_widgets._Warm.poll())


class GStreamerPlayer(unittest.TestCase):
    """The GstPlay is only ever read from a GStreamer MediaFile of the known layout."""
    def test_anything_else_has_no_player(self):
        self.assertIsNone(next_widgets.gst_player(FakeMedia('drive.mp4')))
        self.assertIsNone(next_widgets.gst_player(next_widgets.Gtk.StringObject.new('drive.mp4')))   # a GObject, not a MediaFile


class GStreamerLoadsOffTheGtkThread(unittest.TestCase):
    """A fresh registry scan takes seconds: the GTK thread never waits for it. A scene asked for motion
    meanwhile shows its still, then its loop once GStreamer is known."""
    def setUp(self):
        self.created = []

        def new(path):
            self.created.append(FakeMedia(path))
            return self.created[-1]

        state = {'lock': next_widgets._GST['lock'], 'waiting': [], 'thread': object()}   # loading, not known yet
        for item in [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                     mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                     mock.patch.object(next_widgets, '_GST', state),
                     mock.patch.dict(next_widgets._LOOPS, clear=True)]:
            item.start()
            self.addCleanup(item.stop)

    def test_motion_waits_for_gstreamer_without_blocking(self):
        scene = LoopPoolLimit.scene(self)
        closed = LoopPoolLimit.scene(self)
        self.assertIsNone(next_widgets.gst_ready(wait=False))
        scene.show_room({'still': 'lake.jpg', 'loop': 'lake.mp4'}, True)
        closed.show_room({'still': 'drive.jpg', 'loop': 'drive.mp4'}, True)
        closed.close()
        self.assertEqual(scene.picture.paintable, 'still:lake.jpg')
        self.assertEqual(self.created, [])
        next_widgets._GST['ready'] = True
        next_widgets._gst_known()
        self.assertTrue(scene.playing_motion)
        self.assertEqual([media.path for media in self.created], ['lake.mp4'], 'a closed scene stays closed')


class DecoderThreads(unittest.TestCase):
    """libav's H.264 decoder starts one frame thread per core, each with its own frames: about 120 MB
    per loop. Every libav video decoder any bin in the process adds is capped."""
    def setUp(self):
        if not next_widgets.gst_ready():
            self.skipTest('GStreamer playback is not available here')
        from gi.repository import Gst
        self.Gst = Gst
        if Gst.ElementFactory.find('avdec_h264') is None:
            self.skipTest('gst-libav is not available here')

    def test_a_decoder_added_to_any_bin_is_capped(self):
        Gst = self.Gst
        outer, inner = Gst.Bin.new('outer'), Gst.Bin.new('inner')
        outer.add(inner)
        decoder = Gst.ElementFactory.make('avdec_h264')
        self.assertEqual(decoder.get_property('max-threads'), 0, 'libav default: one thread per core')
        inner.add(decoder)
        self.assertEqual(decoder.get_property('max-threads'), next_widgets.DECODER_THREADS)
        self.assertEqual(next_widgets.DECODER_THREADS, 1)

    def test_other_elements_are_left_alone(self):
        Gst = self.Gst
        holder = Gst.Bin.new('holder')
        queue = Gst.ElementFactory.make('queue')
        holder.add(queue)        # no max-threads property: nothing set, nothing logged
        self.assertIs(queue.get_parent(), holder)


if __name__ == '__main__':
    unittest.main()
