"""Art textures are decoded once, the NVIDIA driver gets GTK's OpenGL renderer, and a dropped scene
loop is torn down only when GStreamer is idle, with its player pinned across the teardown."""
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
    """Records what the scene code does to a Gtk.MediaFile."""
    def __init__(self, path=None):
        self.path, self.error, self.playing, self.prepared = path, None, False, False
        self.calls, self.handlers = [], {}

    def __getattr__(self, name):
        if name in ('play', 'pause', 'clear', 'set_loop', 'set_muted'):
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

    def is_prepared(self):
        return self.prepared

    def get_flags(self):
        return 0

    def connect(self, signal, callback, *data):
        self.handlers[len(self.handlers) + 1] = (signal, callback, data)
        return len(self.handlers)

    def disconnect(self, handler):
        del self.handlers[handler]

    def emit(self, signal):
        for name, callback, data in list(self.handlers.values()):
            if name in (signal, 'notify'):
                callback(self, None, *data)


class SceneLoops(unittest.TestCase):
    """Clearing a Gtk.MediaFile while GstPlay's thread holds a transient reference lets that thread
    free its player and then use it (a segfault in gst_play_main). Loops are shared per file, only
    played and paused while shown, and a hidden loop is torn down once prepared and quiet, with an
    extra GstPlay reference held across clear() and dropped later on the main thread."""
    def setUp(self):
        self.now, self.created, self.timers = 100., [], []
        self.lib = mock.Mock()

        def new(path):
            self.created.append(FakeMedia(path))
            return self.created[-1]

        def timeout_add(interval, callback, *data):
            self.timers.append((interval, callback, data))
            return len(self.timers)
        patches = [mock.patch.object(next_widgets.Gtk.MediaFile, 'new_for_filename', side_effect=new),
                   mock.patch.object(next_widgets, 'gst_ready', return_value=True),
                   mock.patch.object(next_widgets, 'texture_for', side_effect=lambda path: 'still:%s' % path),
                   mock.patch.object(next_widgets.time, 'monotonic', lambda: self.now),
                   mock.patch.object(next_widgets.GLib, 'timeout_add', side_effect=timeout_add),
                   mock.patch.object(next_widgets, '_player', side_effect=lambda media: 0x1000 + self.created.index(media)),
                   mock.patch.dict(next_widgets._ctypes, {'gobject': self.lib}, clear=True),
                   mock.patch.dict(next_widgets._LOOPS, clear=True)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_timers(self):
        pending, self.timers = self.timers, []
        for interval, callback, data in pending:
            if callback(*data):
                self.timers.append((interval, callback, data))

    def scene(self, mapped=True):
        scene = next_widgets.Scene.__new__(next_widgets.Scene)
        scene.picture = FakePicture()
        scene.media = scene.loop = scene.still = scene.key = None
        scene.error_handler, scene.want_motion = 0, False
        scene.mapped, scene.minimized = mapped, False
        scene.get_mapped = lambda: scene.mapped
        return scene

    @staticmethod
    def room(name):
        return {'still': name + '.jpg', 'poster': name + '.jpg', 'loop': name + '.mp4'}

    def test_a_hidden_loop_is_torn_down_only_when_prepared_and_quiet_with_the_player_pinned(self):
        preview = self.scene()
        preview.show_room(self.room('lake'), True)
        media = self.created[0]
        self.assertTrue(preview.playing_motion)
        preview.close()
        self.assertEqual(preview.picture.paintable, 'still:lake.jpg')
        self.assertFalse(media.playing)
        self.now += 5
        self.run_timers()                      # still prerolling: GstPlay is busy, nothing torn down
        self.assertNotIn('clear', media.calls)
        media.prepared = True
        media.emit('notify::prepared')
        self.now += next_widgets.LOOP_QUIET / 2
        self.run_timers()
        self.assertNotIn('clear', media.calls)
        self.now += next_widgets.LOOP_QUIET
        self.run_timers()
        self.assertEqual(media.calls[-1], 'clear')
        self.lib.g_object_ref.assert_called_once_with(0x1000)
        self.lib.g_object_unref.assert_not_called()            # held across clear()
        self.assertEqual(next_widgets._LOOPS, {})
        self.assertEqual(media.handlers, {}, 'nothing may stay connected to a torn-down loop')
        self.run_timers()                                      # the unpin timer
        self.lib.g_object_unref.assert_called_once_with(0x1000)
        self.assertEqual(next_widgets._PINNED, [])

    def test_a_loop_another_scene_shows_is_shared_and_kept(self):
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
        media.prepared = True
        self.now += 10
        self.run_timers()
        self.assertNotIn('clear', media.calls, 'Now still shows it')
        now._visibility(minimized=False)
        self.assertTrue(media.playing and now.playing_motion)

    def test_rapid_switching_never_clears_a_busy_loop(self):
        preview = self.scene()
        for _ in range(3):
            for name in ('lake', 'library', 'drive'):
                preview.show_room(self.room(name), True)
                self.now += .1
                self.run_timers()
        for media in self.created:
            self.assertNotIn('clear', media.calls)
        self.assertEqual(len(self.created), 3, 'a shown or recently hidden loop is reused, not recreated')

    def test_an_unverified_gtk_keeps_the_loop_instead_of_tearing_it_down(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), True)
        media = self.created[0]
        scene.close()
        media.prepared = True
        self.now += 10
        with mock.patch.object(next_widgets, '_player', return_value=None):
            self.run_timers()
        self.assertNotIn('clear', media.calls)
        self.lib.g_object_ref.assert_not_called()

    def test_reduced_motion_shows_the_still_and_creates_no_loop(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), False)
        self.assertIsNone(scene.media)
        self.assertEqual(self.created, [])
        self.assertEqual(scene.picture.paintable, 'still:lake.jpg')

    def test_a_failed_loop_falls_back_to_the_still(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), True)
        media = self.created[0]
        media.error = 'no decoder'
        media.emit('notify::error')
        self.assertIsNone(scene.media)
        self.assertEqual(scene.picture.paintable, 'still:lake.jpg')
        again = self.scene()
        again.show_room(self.room('lake'), True)
        self.assertFalse(again.want_motion)
        self.assertIsNone(again.media)

    def test_closed_scenes_hold_no_handlers_on_shared_objects(self):
        scene = self.scene()
        scene.show_room(self.room('lake'), True)
        media = self.created[0]
        scene.close()
        self.assertEqual([h[0] for h in media.handlers.values()], ['notify'], 'only the pool\'s own handler')


if __name__ == '__main__':
    unittest.main()
