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


class Stylesheets(unittest.TestCase):
    def test_each_stylesheet_is_installed_once_by_the_one_helper(self):
        with mock.patch.object(next_widgets.Gtk.StyleContext, 'add_provider_for_display') as add, \
                mock.patch.dict(next_widgets._STYLES, clear=True):
            first = next_widgets.install_css('display', '.probe-a { color: #fff; }', 2)
            again = next_widgets.install_css('display', '.probe-a { color: #fff; }', 2)
            other = next_widgets.install_css('display', '.probe-b { color: #000; }')
        self.assertIs(first, again)
        self.assertIsNot(first, other)
        self.assertEqual([call.args[2] for call in add.call_args_list],
                         [next_widgets.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2,
                          next_widgets.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION])

    def test_no_module_keeps_its_own_copy(self):
        source = Path(next_widgets.__file__).parent
        copies = [path.name for path in sorted(source.glob('next_*.py'))
                  if path.name != 'next_widgets.py' and 'def install_css' in path.read_text()]
        self.assertEqual(copies, [])


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

    def test_the_cache_keeps_about_64_textures_dropping_the_least_recently_shown(self):
        paths = []
        for index in range(next_widgets.TEXTURE_LIMIT + 6):
            path = os.path.join(self.dir.name, 'art-%d.jpg' % index)
            Path(path).write_bytes(b'not really a jpeg')
            paths.append(path)
        self.assertEqual(next_widgets.TEXTURE_LIMIT, 64)
        first = next_widgets.texture_for(paths[0])
        for path in paths[1:next_widgets.TEXTURE_LIMIT]:
            next_widgets.texture_for(path)
        self.assertIs(next_widgets.texture_for(paths[0]), first, 'a recent texture was decoded again')
        for path in paths[next_widgets.TEXTURE_LIMIT:]:
            next_widgets.texture_for(path)
        self.assertEqual(len(next_widgets._TEXTURES), next_widgets.TEXTURE_LIMIT)
        self.assertIs(next_widgets.texture_for(paths[0]), first, 'the most recently shown texture was dropped')
        loads = self.loader.call_count
        next_widgets.texture_for(paths[1])     # the least recently shown went first: decoded again
        self.assertEqual(self.loader.call_count, loads + 1)

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
