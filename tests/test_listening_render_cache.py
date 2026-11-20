"""Art textures are decoded once, the NVIDIA driver gets GTK's OpenGL renderer, and a dropped scene
loop is never torn down (GStreamer's GstPlay thread can be busy with it)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import next_widgets


class FakePicture:
    """Like FocalPicture: it can be given a paintable but has no get_paintable."""
    def __init__(self):
        self.paintable = None
        self.sets = 0

    def set_paintable(self, paintable):
        self.paintable = paintable
        self.sets += 1


class TextureCache(unittest.TestCase):
    def setUp(self):
        next_widgets._TEXTURES.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'art.jpg')
        Path(self.path).write_bytes(b'not really a jpeg')
        self.loads = mock.patch.object(next_widgets.Gdk.Texture, 'new_from_filename',
                                       side_effect=lambda path: object())
        self.loader = self.loads.start()

    def tearDown(self):
        self.loads.stop()
        self.dir.cleanup()
        next_widgets._TEXTURES.clear()

    def test_repeated_polls_decode_and_set_the_art_once(self):
        picture = FakePicture()
        for _ in range(20):
            next_widgets.show_texture(picture, self.path)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(picture.sets, 1)

    def test_a_changed_file_is_read_again(self):
        first = next_widgets.texture_for(self.path)
        stat = os.stat(self.path)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertIsNot(next_widgets.texture_for(self.path), first)
        self.assertEqual(self.loader.call_count, 2)

    def test_missing_or_empty_path_shows_nothing(self):
        picture = FakePicture()
        next_widgets.show_texture(picture, None)
        next_widgets.show_texture(picture, os.path.join(self.dir.name, 'gone.jpg'))
        self.assertIsNone(picture.paintable)
        self.assertEqual(self.loader.call_count, 0)


class RendererChoice(unittest.TestCase):
    def test_nvidia_driver_gets_opengl(self):
        with tempfile.NamedTemporaryFile() as marker:
            env = {}
            self.assertEqual(next_widgets.choose_renderer(env, marker.name), 'ngl')

    def test_explicit_choice_wins(self):
        with tempfile.NamedTemporaryFile() as marker:
            env = {'GSK_RENDERER': 'vulkan'}
            self.assertEqual(next_widgets.choose_renderer(env, marker.name), 'vulkan')

    def test_other_drivers_keep_gtk_default(self):
        env = {}
        self.assertIsNone(next_widgets.choose_renderer(env, '/nonexistent/nvidia/version'))
        self.assertNotIn('GSK_RENDERER', env)


class FakeMedia:
    """Records what a Scene does to its Gtk.MediaFile."""
    def __init__(self, path=None):
        self.path, self.error, self.playing, self.calls, self.handlers = path, None, False, [], {}

    def __getattr__(self, name):
        if name in ('play', 'pause', 'clear', 'set_loop', 'set_muted', 'unref', 'run_dispose'):
            def call(*args):
                self.calls.append(name)
                if name in ('play', 'pause'):
                    self.playing = name == 'play'
            return call
        raise AttributeError(name)

    def get_error(self):
        return self.error

    def get_playing(self):
        return self.playing

    def connect(self, signal, callback):
        self.handlers[len(self.handlers) + 1] = (signal, callback)
        return len(self.handlers)

    def disconnect(self, handler):
        del self.handlers[handler]


class SharedLoops(unittest.TestCase):
    """Clearing a Gtk.MediaFile while GstPlay's thread is busy lets that thread free its player and then
    use it (a segfault in gst_play_main). Scene loops are created once per file, shared, and only ever
    paused or played: never cleared, never finalized."""
    def setUp(self):
        self.created = []

        def new(path):
            self.created.append(FakeMedia(path))
            return self.created[-1]
        patches = [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                   mock.patch.object(next_widgets, 'gst_ready', return_value=True),
                   mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                   mock.patch.dict(next_widgets._LOOPS, clear=True),
                   mock.patch.dict(next_widgets._WANTED, clear=True)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def scene(self, mapped=True):
        scene = next_widgets.Scene.__new__(next_widgets.Scene)
        scene.picture = FakePicture()
        scene.media, scene.error_handler, scene.still, scene.key, scene.want_motion = None, 0, None, None, False
        scene.mapped, scene.minimized = mapped, False
        scene.get_mapped = lambda: scene.mapped
        return scene

    @staticmethod
    def room(name):
        return {'still': name + '.jpg', 'poster': name + '.jpg', 'loop': name + '.mp4'}

    def test_switching_rooms_and_closing_never_clears_a_loop(self):
        preview = self.scene()
        for _ in range(3):
            for name in ('lake', 'library', 'lake'):
                preview.show_room(self.room(name), True)
                self.assertTrue(preview.playing_motion)
                preview.key = None
        preview.close()
        self.assertEqual([m.path for m in self.created], ['lake.mp4', 'library.mp4'])
        for media in self.created:
            self.assertNotIn('clear', media.calls)
            self.assertFalse(media.playing, 'nothing wants the loop any more: it must be paused')
            self.assertEqual(media.handlers, {}, 'a closed scene must not keep handlers on a shared loop')
        self.assertEqual(preview.picture.paintable, 'still:lake.jpg')

    def test_a_shared_loop_plays_while_any_scene_wants_it(self):
        now, preview = self.scene(), self.scene()
        now.show_room(self.room('lake'), True)
        preview.show_room(self.room('lake'), True)
        media = self.created[0]
        self.assertEqual(len(self.created), 1)
        now._visibility(minimized=True)               # the window is minimized: Now stops wanting it
        self.assertTrue(media.playing)
        self.assertFalse(now.playing_motion)
        self.assertTrue(preview.playing_motion)
        preview.close()
        self.assertFalse(media.playing)
        now._visibility(minimized=False)
        self.assertTrue(media.playing and now.playing_motion)

    def test_reduced_motion_shows_the_still_and_creates_no_loop(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), False)
        self.assertIsNone(scene.media)
        self.assertEqual(self.created, [])
        self.assertEqual(scene.picture.paintable, 'still:lake.jpg')

    def test_a_failed_loop_falls_back_to_the_still_and_stays_still(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), True)
        media = self.created[0]
        media.error = 'no decoder'
        for signal, callback in list(media.handlers.values()):
            callback(media, None)
        self.assertIsNone(scene.media)
        self.assertEqual(scene.picture.paintable, 'still:lake.jpg')
        again = self.scene()
        again.show_room(self.room('lake'), True)
        self.assertFalse(again.want_motion)
        self.assertIsNone(again.media)
        self.assertNotIn('clear', media.calls)


if __name__ == '__main__':
    unittest.main()
