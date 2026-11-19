"""Window smoke test: the real main window, built on a private Xvfb with fixtures.

Shows each top-level surface (Rooms, a room preview, Listen, Now and the Mix, Preferences) and
fails if any raises or the window never leaves its loading spinner. Also checks that a surface
whose render raises is isolated (an inline error, the rest still drawn), the keyboard rules of
Now, and the "room service isn't running" banner. Runs in the package checkPhase: it re-executes
itself under xvfb-run and dbus-run-session (see tests/probes/o6_env.py), with a private HOME, XDG
dirs and runtime dir, and nothing reaches the desktop session, the network or real credentials.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'probes'))
sys.path.insert(0, str(HERE))
if '--output' not in sys.argv:
    sys.argv += ['--output', tempfile.mkdtemp(prefix='listening-smoke-')]
import o6_env  # noqa: E402  (re-executes this file on Xvfb when there is no private display yet)

ENV = o6_env.Env(__doc__)


def mapped(widget):
    return widget is not None and widget.get_mapped() and widget.get_width() > 0 and widget.get_height() > 0


class WindowSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = ENV.__enter__()
        cls.Gtk, cls.Gdk = ENV.Gtk, ENV.Gdk

    @classmethod
    def tearDownClass(cls):
        ENV.__exit__(None, None, None)

    def tearDown(self):
        for window in list(self.env.app.get_windows()):
            window.close()
        self.env.pump(.3)

    def assertDrawn(self, window):
        self.assertNotEqual(window.current_page(), 'loading', 'the window never left its loading spinner')
        self.assertEqual(window.section_errors, {}, 'a surface raised while drawing')

    def key(self, window, keyval):
        """Deliver a key the way GTK does: capture-phase controllers first, then the rest."""
        controllers = [c for c in window.observe_controllers() if isinstance(c, self.Gtk.EventControllerKey)]
        controllers.sort(key=lambda c: c.get_propagation_phase() != self.Gtk.PropagationPhase.CAPTURE)
        for controller in controllers:
            if controller.emit('key-pressed', keyval, 0, 0):
                return True
        return False

    def test_every_surface_draws(self):
        env = self.env
        window = env.launch(page='rooms')
        self.assertDrawn(window)
        self.assertTrue(mapped(window.rooms.widget), 'Rooms is not shown')
        cards = list(window.rooms.card_for.values())
        self.assertTrue(cards, 'Rooms shows no room cards')
        # A room preview.
        window.rooms.open_preview(cards[0])
        env.pump(.6)
        self.assertTrue(mapped(window.rooms.preview_widgets['enter']), 'the preview has no Enter room button')
        self.assertTrue(window.rooms.close_preview(animate=False))
        # Listen.
        window.show_listen()
        env.pump(.8)
        self.assertTrue(mapped(window.listen.search), 'Listen is not shown')
        self.assertTrue(env.buttons(window.listen.index_body, 'Tune in'), 'Listen shows nothing to tune in to')
        # Now and the Mix.
        env.start_station_room()
        window.show_now()
        env.pump(.8)
        self.assertEqual(window.current_page(), 'now')
        self.assertTrue(mapped(window.play), 'Now has no play control')
        self.assertIs(window.get_focus(), window.play, 'entering Now does not focus play')
        window.open_mix()
        env.pump(.8)
        self.assertTrue(window.sheet.get_open())
        self.assertTrue(all(mapped(row) for row in window.mix.rows.values()), 'a Mix row is not shown')
        window.sheet.set_open(False)
        env.pump(.4)
        # Preferences.
        window.open_preferences()
        env.pump(.8)
        self.assertTrue(mapped(window.preferences), 'Preferences did not open')
        window.preferences.close()
        env.pump(.3)
        self.assertDrawn(window)
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_a_surface_that_raises_is_isolated(self):
        env = self.env
        import next_rooms

        def broken(_self, _snapshot):
            raise RuntimeError('fixture: rooms cannot draw')
        with mock.patch.object(next_rooms.RoomsPage, 'render', broken), \
                mock.patch('sys.stderr', new_callable=lambda: open(os.devnull, 'w')):
            window = env.launch(page=None)
            self.assertNotEqual(window.current_page(), 'loading', 'one failing surface blanked the window')
            self.assertIn('rooms', window.section_errors)
            self.assertTrue(window.section_bar.get_reveal_child())
            self.assertIn('Rooms couldn’t be drawn', window.section_title.get_text())
            window.show_listen()
            env.pump(.6)
            self.assertTrue(env.buttons(window.listen.index_body, 'Tune in'), 'Listen stopped drawing too')
        env.pump(1.2)
        self.assertNotIn('rooms', window.section_errors, 'the inline error stays after Rooms draws again')

    def test_space_and_arrows_in_now_whatever_has_focus(self):
        env = self.env
        env.start_station_room()
        window = env.launch(page=None)
        self.assertEqual(window.current_page(), 'now', 'launching into a playing room shows Now')
        env.pump(.4)
        self.assertIs(window.get_focus(), window.play, 'launching into Now does not focus play')
        window.toast('A toast with a button', 'Undo', lambda: None)
        env.pump(.6)
        self.assertIs(window.get_focus(), window.play, 'a toast took the focus')
        window.back.grab_focus()   # Space must not press Back
        mark = len(env.log)
        self.assertTrue(self.key(window, self.Gdk.KEY_space))
        env.settle(.8)
        self.assertEqual([a for a, _ in env.commands(mark)], ['quiet'])
        self.assertEqual(window.current_page(), 'now')
        before = window.volume.get_value()
        self.assertTrue(self.key(window, self.Gdk.KEY_Down))
        self.assertLess(window.volume.get_value(), before)
        window.volume.grab_focus()   # a focused slider keeps its own keys
        self.assertFalse(window.key_captured(None, self.Gdk.KEY_space, 0, 0))
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_an_unreachable_room_service_says_so_and_offers_start_again(self):
        env = self.env
        window = env.launch(page='rooms')
        import session_client
        calls = []
        with mock.patch.object(session_client, 'request', side_effect=ConnectionRefusedError(111, 'Connection refused')), \
                mock.patch.object(session_client, 'ensure_service', side_effect=lambda *a, **k: calls.append(1)):
            window.poll()
            env.pump(1.0)
            self.assertTrue(window.offline_bar.get_reveal_child())
            self.assertIn('isn’t running', window.offline_title.get_text())
            self.assertNotIn('Errno', window.offline_title.get_text())
            self.assertFalse(window.timer_button.get_sensitive())
            window.offline_start.emit('clicked')
            env.pump(.8)
            self.assertEqual(calls, [1], 'Start again does not use ensure_service')
        env.pump(1.5)
        self.assertFalse(window.offline_bar.get_reveal_child(), 'the banner stays after the service answers')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
