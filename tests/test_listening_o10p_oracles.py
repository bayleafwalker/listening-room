"""Independent acceptance oracles for O10-PERF criterion 3: the test environment plays real scene media.

Owned by the O10-PERF acceptance author. Before O10 the dev shell had no GStreamer, so
`scripts/listening-dev.sh test` and the acceptance probes never played a scene loop: the
window fell back to still art and media lifetime and cost were never exercised. These
checks run wherever the suites run (the dev shell and the package checkPhase) and fail
when the environment cannot play the catalogue's loops. The GUI side (the dev shell's
GStreamer is the package's own, a preview and an entered room visibly play) is in
tests/probes/listening-o10p-acceptance.py.

    python3 tests/test_listening_o10p_oracles.py
"""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LISTENING_HUB_SOURCE', ROOT / 'src/listening_hub.py')).parent
sys.path.insert(0, str(SOURCE))

# The elements a catalogue loop (H.264 in MP4) needs, and what Gtk.MediaFile plays it with.
NEEDED = ('playbin3', 'qtdemux', 'h264parse', 'avdec_h264', 'videoconvert')


def assets():
    """The built assets: the dev script exports LISTENING_HUB_ASSETS; the package build links ./assets."""
    for candidate in (os.environ.get('LISTENING_HUB_ASSETS'), ROOT / 'assets'):
        if candidate and (Path(candidate) / 'room-night-drive.mp4').is_file():
            return Path(candidate)
    return None


def gst():
    import gi
    try:
        gi.require_version('Gst', '1.0')
    except ValueError as error:
        raise AssertionError('GStreamer is not in this environment (%s): scene loops cannot play here; '
                             'GST_PLUGIN_SYSTEM_PATH_1_0=%r' % (error, os.environ.get('GST_PLUGIN_SYSTEM_PATH_1_0')))
    from gi.repository import Gst
    Gst.init(None)
    return Gst


class TestMediaInTheDevLoop(unittest.TestCase):
    def test_gstreamer_has_the_elements_the_loops_need(self):
        Gst = gst()
        missing = [name for name in NEEDED if Gst.ElementFactory.find(name) is None]
        self.assertEqual(missing, [], 'missing GStreamer elements: %s' % missing)

    def test_the_window_would_play_scene_loops_here(self):
        """The window's own gate: without it every Scene shows the still and no media is ever created."""
        gst()
        import next_widgets
        self.assertTrue(next_widgets.gst_ready(), 'next_widgets.gst_ready() is False in the test environment')

    def test_a_catalogue_loop_decodes_end_to_end(self):
        folder = assets()
        if folder is None:
            self.skipTest('no built assets here (set LISTENING_HUB_ASSETS)')
        Gst = gst()
        loop = folder / 'room-night-drive.mp4'
        pipeline = Gst.parse_launch('filesrc name=src ! qtdemux ! h264parse ! avdec_h264 ! '
                                    'fakesink name=sink signal-handoffs=true sync=false')
        pipeline.get_by_name('src').set_property('location', str(loop))
        frames = [0]
        pipeline.get_by_name('sink').connect('handoff', lambda *_: frames.__setitem__(0, frames[0] + 1))
        pipeline.set_state(Gst.State.PLAYING)
        message = pipeline.get_bus().timed_pop_filtered(60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipeline.set_state(Gst.State.NULL)
        self.assertIsNotNone(message, 'decoding %s timed out' % loop.name)
        self.assertEqual(message.type, Gst.MessageType.EOS,
                         message.parse_error() if message.type == Gst.MessageType.ERROR else message.type)
        self.assertEqual(frames[0], 480, 'decoded %d of the 480 frames of %s' % (frames[0], loop.name))


if __name__ == '__main__':
    unittest.main()
