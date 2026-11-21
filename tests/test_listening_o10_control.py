"""O10 control: the room's transport does what it says, in the real window.

A failed start keeps its card on Now, named after the room it tried; connecting shows only what the
target can have; each Mix row pauses, resumes and stops its own layer; no ⋯ menu holds one item.
Runs like the window smoke test: a private Xvfb, D-Bus, HOME and XDG dirs, a fake backend, no audio.
"""
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'probes'))
sys.path.insert(0, str(HERE))
if '--output' not in sys.argv:
    sys.argv += ['--output', tempfile.mkdtemp(prefix='listening-o10c-')]
import o6_env  # noqa: E402  (re-executes this file on Xvfb when there is no private display yet)

ENV = o6_env.Env(__doc__)


class ControlWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = ENV.__enter__()
        import room_catalogue
        rooms = room_catalogue.rooms()
        cls.first, cls.second = rooms[0], rooms[1]

    @classmethod
    def tearDownClass(cls):
        ENV.__exit__(None, None, None)

    def tearDown(self):
        env = self.env
        env.start_error, env.start_delay, env.atmosphere_error = None, 0., None
        env.backend.fail_picture = False
        for window in list(env.app.get_windows()):
            window.close()
        env.pump(.3)
        if env.controller.active:
            env.service.execute('stop', {})
            env.settle(.6)

    def enter_and_wait(self, window, room):
        window.start('enter', {'room_id': room['id']}, room['name'])
        self.env.settle(1.5)

    def card(self, window):
        return window.pending_card.get_visible(), window.pending_title.get_text(), window.pending_detail.get_text()

    # ---- N2 ---------------------------------------------------------------------------------------

    def nothing_starts(self):
        env = self.env
        env.start_error, env.atmosphere_error = 'Connection refused', 'Sound could not load'
        env.backend.fail_picture = True

    def test_from_idle_a_room_whose_music_fails_is_entered_with_the_failure_on_music(self):
        # Coordinator decision (criterion 1): each layer fails on its own, on enter too.
        env = self.env
        window = env.launch(page='rooms')
        env.start_error = 'Connection refused'
        self.enter_and_wait(window, self.second)
        env.pump(1.0)
        self.assertEqual(window.current_page(), 'now')
        self.assertIsNone(window.pending, 'a target-named card although the rest of the room started')
        self.assertFalse(window.pending_card.get_visible())
        self.assertEqual(env.controller.active['name'], self.second['name'])
        self.assertEqual(window.room_name.get_text(), self.second['name'])
        self.assertTrue(window.banner.get_visible())
        self.assertEqual(window.banner_title.get_text(), 'Music couldn’t start')
        self.assertTrue(window.banner_retry.get_visible())
        window.open_mix()
        env.pump(.8)
        self.assertTrue(window.mix.rows['music'].retry.get_mapped(), 'the Music row offers Try again')

    def test_a_failed_enter_from_idle_stays_on_now_named_after_the_room(self):
        env = self.env
        window = env.launch(page='rooms')
        self.nothing_starts()
        self.enter_and_wait(window, self.second)
        env.pump(1.0)
        visible, title, detail = self.card(window)
        self.assertEqual(window.current_page(), 'now', 'a failed enter returned to Rooms')
        self.assertTrue(visible, 'no failure card')
        self.assertEqual(title, 'Couldn’t start ' + self.second['name'])
        self.assertIn('could not connect', detail)
        self.assertNotIn('room service', detail)
        self.assertTrue(window.pending_retry.get_visible() and window.pending_retry.get_sensitive())
        self.assertTrue(window.pending_back.get_visible())
        self.assertIsNone(env.controller.active)
        # Try again sends the same start; Back returns where the start came from.
        mark = len(env.log)
        window.retry_pending()
        env.settle(1.5)
        self.assertEqual([a for a, _ in env.commands(mark)], ['enter'])
        self.assertEqual(self.card(window)[1], 'Couldn’t start ' + self.second['name'])
        window.dismiss_pending()
        env.pump(.6)
        self.assertEqual(window.current_page(), 'browse')
        self.assertFalse(window.pending_card.get_visible())

    def test_a_failed_enter_while_a_room_plays_names_the_target_and_keeps_the_room(self):
        env = self.env
        window = env.launch(page='rooms')
        self.enter_and_wait(window, self.first)
        self.assertEqual(env.controller.active['name'], self.first['name'])
        playing = env.backend.state['music']['source_id']
        env.start_error = 'Connection refused'
        self.enter_and_wait(window, self.second)
        env.pump(1.0)
        visible, title, detail = self.card(window)
        self.assertTrue(visible)
        self.assertEqual(title, 'Couldn’t start ' + self.second['name'])
        self.assertIn(self.first['name'] + ' is still there', detail)
        self.assertEqual(env.controller.active['name'], self.first['name'])
        self.assertEqual(env.backend.state['music']['source_id'], playing)
        self.assertFalse(window.banner.get_visible(), 'the old room shows the new room’s failure as its own')
        window.dismiss_pending()
        env.pump(.6)
        self.assertEqual(window.current_page(), 'now')
        self.assertEqual(window.room_name.get_text(), self.first['name'])
        self.assertFalse(window.banner.get_visible())

    def test_a_slow_start_that_fails_after_the_grace_time_is_not_silent(self):
        env = self.env
        window = env.launch(page='rooms')
        self.nothing_starts()
        env.start_delay = 2.
        from unittest import mock
        with mock.patch.object(env.next_window, 'START_GRACE_S', .8):
            window.start('enter', {'room_id': self.second['id']}, self.second['name'])
            env.pump(1.4)
            self.assertEqual(window.pending['state'], 'connecting', 'a slow start the controller accepted is not a failure')
            env.settle(2.0)
        self.assertEqual(window.current_page(), 'now')
        self.assertEqual(self.card(window)[1], 'Couldn’t start ' + self.second['name'])

    def test_a_failed_music_retry_in_a_playing_room_shows_on_its_layer(self):
        env = self.env
        env.start_station_room()
        env.service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                                      'name': 'Rain on glass'})
        env.settle(1.0)
        window = env.launch(page=None)
        env.backend.fail_music = True
        try:
            env.service.execute('layer', {'layer': 'music', 'action': 'retry'})
            env.settle(1.2)
        finally:
            env.backend.fail_music = False
        self.assertFalse(window.pending_card.get_visible())
        self.assertTrue(window.banner.get_visible())
        self.assertEqual(window.banner_title.get_text(), 'Music couldn’t start')
        self.assertIn('Ambience', window.banner_detail.get_text())
        self.assertEqual(env.backend.state['atmosphere']['status'], 'Playing')

    # ---- N9 ---------------------------------------------------------------------------------------

    def test_connecting_shows_no_track_controls_and_the_stored_volume(self):
        env = self.env
        env.service.execute('room_volume', {'value': 60})
        window = env.launch(page='rooms')
        self.assertEqual(window.volume.get_value(), 60, 'the slider reads the stored volume before any room')
        env.start_delay = 2.
        window.start('enter', {'room_id': self.first['id']}, self.first['name'])
        env.pump(.6)
        self.assertEqual(window.pending['state'], 'connecting')
        for control in (window.skip, window.less, window.more):
            self.assertFalse(control.get_visible(), control.get_tooltip_text())
        self.assertEqual(window.volume.get_value(), 60)
        self.assertEqual(window.mix_summary.get_text(), 'Connecting…')
        env.settle(2.5)
        self.assertIsNone(window.pending)

    # ---- E and N10 --------------------------------------------------------------------------------

    def test_each_mix_row_pauses_resumes_and_stops_its_own_layer(self):
        env = self.env
        env.start_station_room()
        env.service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                                      'name': 'Rain on glass'})
        env.settle(1.0)
        window = env.launch(page=None)
        window.open_mix()
        env.pump(.8)
        music, ambience = window.mix.rows['music'], window.mix.rows['atmosphere']
        for row, word in ((music, 'music'), (ambience, 'ambience')):
            self.assertTrue(row.playing.get_mapped() and row.halt.get_mapped(), word)
            self.assertEqual(row.playing.get_tooltip_text(), 'Pause ' + word)
            self.assertEqual(row.halt.get_tooltip_text(), 'Stop ' + word)
        mark = len(env.log)
        music.playing.emit('clicked')
        env.settle(.8)
        self.assertEqual(env.commands(mark), [('layer', {'layer': 'music', 'action': 'pause'})])
        self.assertEqual(env.backend.state['music']['status'], 'Paused')
        self.assertEqual(env.backend.state['atmosphere']['status'], 'Playing')
        self.assertEqual(music.playing.get_tooltip_text(), 'Play music')
        self.assertTrue(music.subtitle.has_css_class('accent'))
        self.assertIn('paused', music.subtitle.get_text())
        self.assertEqual(window.play.get_tooltip_text(), 'Pause the room', 'ambience still sounds')
        # Room Pause then Play: the music paused on its own stays paused.
        window.toggle_room()
        env.settle(.8)
        self.assertEqual(env.backend.state['atmosphere']['status'], 'Paused')
        window.toggle_room()
        env.settle(.8)
        self.assertEqual((env.backend.state['music']['status'], env.backend.state['atmosphere']['status']),
                         ('Paused', 'Playing'))
        music.playing.emit('clicked')
        env.settle(.8)
        self.assertEqual(env.backend.state['music']['status'], 'Playing')
        mark = len(env.log)
        ambience.halt.emit('clicked')
        env.settle(.8)
        self.assertEqual(env.commands(mark), [('layer', {'layer': 'atmosphere', 'action': 'stop'})])
        self.assertEqual(env.backend.state['music']['status'], 'Playing')

    def test_no_overflow_menu_holds_a_single_item(self):
        env = self.env
        env.start_station_room()
        window = env.launch(page=None)
        window.open_mix()
        env.pump(.8)
        Gtk = env.Gtk
        menus = [w for w in env.widgets(window) if isinstance(w, Gtk.MenuButton) and w.get_mapped()
                 and w.get_icon_name() == 'view-more-symbolic']
        self.assertTrue(menus, 'the live radio Mix rows have no menu at all')
        for menu in menus:
            menu.popup()
            env.pump(.3)
            items = [w for w in env.widgets(menu.get_popover()) if isinstance(w, Gtk.Button) and w.get_mapped()]
            menu.popdown()
            env.pump(.1)
            self.assertGreater(len(items), 1, [w.get_label() for w in items])
            self.assertIn('Stop', [w.get_label() for w in items])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
