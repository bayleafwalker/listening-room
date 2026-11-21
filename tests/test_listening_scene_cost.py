"""What scene loops cost the window: the loop pool has a limit, and libav decoders run one thread
instead of one per core."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_widgets
from test_listening_render_cache import FakeMedia, FakePicture


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
