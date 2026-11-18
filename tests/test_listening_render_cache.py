"""Art textures are decoded once, and the NVIDIA driver gets GTK's OpenGL renderer."""
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


if __name__ == '__main__':
    unittest.main()
