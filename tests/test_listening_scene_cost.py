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
