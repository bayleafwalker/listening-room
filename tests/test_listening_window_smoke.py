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
import time
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

    def test_slow_and_competing_starts_are_never_silent_or_doubled(self):
        env = self.env
        window = env.launch(page='listen')
        keys = [t for _k, t in window.listen.scope_list]
        window.listen.scopes.set_active(keys.index('Radio'))
        env.pump(.6)
        import session_client
        import time
        answer = session_client.request   # the fixture's request double

        def slow(action, arguments=None, *rest, **kw):
            if action in ('music', 'link', 'station.start'):
                time.sleep(2.5)           # the controller answers after the grace time
            return answer(action, arguments, *rest, **kw)
        with mock.patch.object(env.next_window, 'START_GRACE_S', 1.0), \
                mock.patch.object(session_client, 'request', side_effect=slow):
            mark = len(env.log)
            env.buttons(window.listen.index_body, 'Tune in')[0].emit('clicked')
            env.pump(.3)
            self.assertFalse(window.start('link', {'url': 'https://radio.example.test/other', 'destination': 'music'}, 'Other'))
            self.assertTrue(any(t.startswith('Still connecting to') for t in env.texts(window.toasts)),
                            'a competing start was dropped without a word')
            env.pump(1.4)
            self.assertEqual(window.pending['state'], 'failed', 'the slow start never showed it is taking long')
            self.assertFalse(window.pending_retry.get_sensitive(), 'Try again is offered while the first start runs')
            window.retry_pending()
            env.pump(2.5)
            env.settle(1.0)
            starts = [a for a, _ in env.commands(mark) if a in ('music', 'link', 'station.start')]
            self.assertEqual(len(starts), 1, starts)
            self.assertIsNone(window.pending, 'the late success did not clear the failure card')
            self.assertTrue(env.controller.active)
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


    def test_a_stale_poll_after_a_save_does_not_undo_it_on_the_next_choice(self):
        """Quick choices in Listen: a poll sent before a save finished must not hand the page the old station,
        or the next choice is built on stale preferences and the earlier one is lost (o3 4p and 4s2)."""
        import next_station
        env = self.env
        env.start_station_room()
        window = env.launch(page='listen')
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        window.listen.show_station(station)
        env.pump(.6)
        stale = dict(window.snapshot)        # what a poll already in flight would still return
        stale_serial = window.polls_sent     # ... and that poll was sent before the save
        groups = window.listen.mood_groups

        def choose(key):
            current = next_station.choices(window.listen.station)[key]
            mood = next(m for m in next_station.MOODS if m['key'] == key)
            index = next(i for i, (text, _value) in enumerate(mood['choices'])
                         if text != current and i != groups[key].get_active())
            groups[key].set_active(index)
            deadline = time.monotonic() + 5
            while window.listen.pending_station is not None and time.monotonic() < deadline:
                env.pump(.05)
            return mood['choices'][index][0]

        # The window's poll timer still runs; the stale read is labelled with the poll that fetched it.
        with mock.patch.object(window, 'poll', lambda: True):
            energy = choose('energy')
            window.render_poll(stale, None, stale_serial)
            familiarity = choose('familiarity')
        env.settle(.5)
        stored = next_station.choices(next(s for s in env.controller.stations() if s['id'] == station['id']))
        self.assertEqual((stored['energy'], stored['familiarity']), (energy, familiarity),
                         'a later choice undid an earlier one')

    def test_a_slow_save_keeps_the_choice_shown_and_the_next_choice_builds_on_it(self):
        """A save slower than any timeout the page might use (a busy controller) must not let a poll put the old
        station back, and a second choice made meanwhile must keep the first."""
        import next_station
        import session_client
        env = self.env
        env.start_station_room()
        window = env.launch(page='listen')
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        window.listen.show_station(station)
        env.pump(.6)
        groups = window.listen.mood_groups
        real = session_client.request

        def pick(key):
            current = next_station.choices(window.listen.station)[key]
            mood = next(m for m in next_station.MOODS if m['key'] == key)
            index = next(i for i, (text, _value) in enumerate(mood['choices'])
                         if text != current and i != groups[key].get_active())
            groups[key].set_active(index)
            return mood['choices'][index][0]

        def slow(action, arguments=None, *rest, **kw):
            if action == 'station.save':
                time.sleep(3.5)
            return real(action, arguments, *rest, **kw)

        with mock.patch.object(session_client, 'request', side_effect=slow):
            energy = pick('energy')
            seen = []
            deadline = time.monotonic() + 3.3
            while time.monotonic() < deadline:
                window.poll()
                env.pump(.1)
                seen.append(next_station.choices(window.listen.station)['energy'])
            familiarity = pick('familiarity')
            deadline = time.monotonic() + 12
            while window.listen.pending_station is not None and time.monotonic() < deadline:
                env.pump(.1)
        env.settle(.5)
        self.assertEqual(set(seen), {energy}, 'the page fell back to the pre-save station during a slow save')
        stored = next_station.choices(next(s for s in env.controller.stations() if s['id'] == station['id']))
        self.assertEqual((stored['energy'], stored['familiarity']), (energy, familiarity),
                         'a later choice undid an earlier one')

    def test_a_second_choice_made_while_a_slow_save_answers_late_is_saved_too(self):
        """The controller applies a choice but answers late; a second choice made meanwhile (another mood, or the
        same mood again) must be shown, sent and stored, and the first must stay shown until then."""
        for first_key, second_key in (('energy', 'familiarity'), ('familiarity', 'familiarity')):
            with self.subTest(first=first_key, second=second_key):
                self.second_choice_during_a_late_save(first_key, second_key)

    def second_choice_during_a_late_save(self, first_key, second_key):
        import next_station
        import session_client
        env = self.env
        env.start_station_room()
        window = env.launch(page='listen')
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        window.listen.show_station(station)
        env.pump(.6)
        groups = window.listen.mood_groups
        real = session_client.request

        def pick(key):
            current = next_station.choices(window.listen.station)[key]
            mood = next(m for m in next_station.MOODS if m['key'] == key)
            index = next(i for i, (text, _value) in enumerate(mood['choices'])
                         if text != current and i != groups[key].get_active())
            groups[key].set_active(index)
            return mood['choices'][index][0]

        def late(action, arguments=None, *rest, **kw):
            answer = real(action, arguments, *rest, **kw)
            if action == 'station.save':
                time.sleep(4)             # applied at once, answered late
            return answer

        mark = len(env.log)
        with mock.patch.object(session_client, 'request', side_effect=late):
            first = pick(first_key)
            seen = []
            for _ in range(8):
                window.poll()
                env.pump(.1)
                seen.append(next_station.choices(window.listen.station)[first_key])
            second = pick(second_key)
            self.assertEqual(next_station.choices(window.listen.station)[second_key], second)
            deadline = time.monotonic() + 15
            while window.listen.pending_station is not None and time.monotonic() < deadline:
                window.poll()
                env.pump(.1)
        env.settle(.5)
        saves = [args['station'] for action, args in env.log[mark:] if action == 'station.save']
        self.assertEqual(len(saves), 2, 'the second choice was never sent')
        self.assertEqual(set(seen), {first}, 'the first choice was not shown until the second')
        stored = next_station.choices(next(s for s in env.controller.stations() if s['id'] == station['id']))
        want = {first_key: first, second_key: second}
        self.assertEqual({key: stored[key] for key in want}, want, 'a choice was lost')
        self.assertEqual({key: next_station.choices(window.listen.station)[key] for key in want}, want)
        window.close()
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_the_start_card_never_covers_the_chip_or_the_room_name_in_a_short_window(self):
        import session_client
        env = self.env
        env.start_station_room()          # a room is up: its chip and name are what the card could cover
        window = env.launch(width=1000, height=460, page='rooms')
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        answer = session_client.request

        def late(action, *rest, **kw):
            if action == 'station.start':
                time.sleep(2.5)
            return answer(action, *rest, **kw)

        def bounds(widget):
            if not widget.get_mapped():
                return None
            ok, rect = widget.compute_bounds(window)
            return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height())

        def overlap(a, b):
            return bool(a and b) and a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
        with mock.patch.object(env.next_window, 'START_GRACE_S', 1.0), \
                mock.patch.object(session_client, 'request', side_effect=late):
            window.start('station.start', {'id': station['id']}, 'Morning lift')
            for state in ('connecting', 'failed'):
                deadline = time.monotonic() + 4
                while (window.pending or {}).get('state') != state and time.monotonic() < deadline:
                    env.pump(.05)
                env.pump(.3)
                card = bounds(window.pending_card)
                self.assertIsNotNone(card, 'the %s card is not shown' % state)
                for widget in (window.chip, window.room_name):
                    self.assertFalse(overlap(card, bounds(widget)), 'the %s card covers %r' % (state, widget.get_text()))
            window.dismiss_pending()
            env.pump(3.0)
        env.settle(.5)
        self.assertTrue(window.room_name.get_mapped(), 'the room name did not come back after the card')
        window.close()
        env.service.execute('leave', {})
        env.settle(1.0)

    # ---- pushed snapshots (WP2-WINDOW criterion 5) ---------------------------------------------

    def fake_stream(self):
        """A stand-in for session_client.subscribe: the test pushes updates from another thread."""
        import collections
        import threading
        update = collections.namedtuple('Update', 'snapshot revision session connected error')
        streams = []

        class FakeSubscription:
            def __init__(self, callback):
                self.callback, self.closed, self.revision = callback, False, 0

            def close(self):
                self.closed = True

            def push(self, snapshot=None, connected=True):
                self.revision += 1
                item = update(snapshot, self.revision, 'fixture', True, None) if connected else \
                    update(None, None, None, False, 'fixture: the stream went away')
                thread = threading.Thread(target=self.callback, args=(item,))
                thread.start()
                thread.join()

        def subscribe(callback, fields=None, path=None, start=False, backoff=(.25, 10.)):
            self.assertFalse(start, 'the window must never start the controller through the stream')
            streams.append(FakeSubscription(callback))
            return streams[-1]
        return streams, subscribe

    def inspects(self, mark):
        return len([action for action, _args in self.env.log[mark:] if action == 'inspect'])

    def test_pushed_snapshots_reach_the_window_promptly_and_polling_is_only_a_fallback(self):
        import session_client
        env = self.env
        env.start_station_room()
        streams, subscribe = self.fake_stream()
        with mock.patch.object(session_client, 'subscribe', side_effect=subscribe, create=True), \
                mock.patch.object(env.next_window, 'FALLBACK_POLL_MS', 1500):
            window = env.launch(page=None)
            self.assertEqual(len(streams), 1, 'the window did not subscribe')
            stream = streams[0]
            stream.push(env.service.execute('inspect', {}))
            env.pump(.4)
            mark = len(env.log)
            env.pump(2.0)
            self.assertEqual(self.inspects(mark), 0, 'the window kept polling while the stream was up')
            # A change made elsewhere arrives by push, promptly.
            env.service.execute('room_volume', {'value': 23})
            stream.push(env.service.execute('inspect', {}))
            started = time.monotonic()
            while round(window.volume.get_value()) != 23 and time.monotonic() - started < 2:
                env.pump(.005)
            elapsed = time.monotonic() - started
            self.assertEqual(round(window.volume.get_value()), 23)
            self.assertLess(elapsed, .25, 'a pushed change took %.3f s to reach Now' % elapsed)
            # The stream goes down: one read at once, then polls no more often than the fallback interval.
            mark = len(env.log)
            stream.push(connected=False)
            env.pump(4.0)
            self.assertIn(self.inspects(mark), (3, 4), 'fallback polling at 1.5 s for 4 s')
            # It comes back: polling stops again.
            env.service.execute('room_volume', {'value': 41})
            stream.push(env.service.execute('inspect', {}))
            env.pump(.4)
            self.assertEqual(round(window.volume.get_value()), 41)
            mark = len(env.log)
            env.pump(2.0)
            self.assertEqual(self.inspects(mark), 0)
            window.close()
            env.pump(.3)
            self.assertTrue(stream.closed, 'closing the window left the stream open')
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_a_change_reaches_the_window_through_the_real_stream_promptly(self):
        """End to end with the real session_client.subscribe and session_service server on a private socket."""
        import functools
        import session_client
        import session_service
        import threading
        env = self.env
        env.start_station_room()
        folder = Path(tempfile.mkdtemp(prefix='listening-smoke-stream-'))
        folder.chmod(0o700)
        path = folder / 'session.sock'
        service = session_service.Service(env.controller)
        server, descriptor = session_service.make_server(path, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        subscribe = functools.partial(session_client.subscribe, path=str(path), backoff=(.02, .1))
        try:
            with mock.patch.object(session_client, 'subscribe', subscribe):
                window = env.launch(page=None)
                deadline = time.monotonic() + 3
                while not window.stream_up and time.monotonic() < deadline:
                    env.pump(.05)
                self.assertTrue(window.stream_up, 'the stream never connected')
                env.pump(.5)
                mark = len(env.log)
                env.service.execute('room_volume', {'value': 29})
                started = time.monotonic()
                while round(window.volume.get_value()) != 29 and time.monotonic() - started < 3:
                    env.pump(.005)
                elapsed = time.monotonic() - started
                self.assertEqual(round(window.volume.get_value()), 29)
                self.assertLess(elapsed, .3, 'the pushed change took %.3f s to reach Now' % elapsed)
                self.assertEqual(self.inspects(mark), 0, 'the change came by a poll, not the stream')
                window.close()
                env.pump(.3)
        finally:
            service.close()
            server.shutdown()
            server.server_close()
            path.unlink(missing_ok=True)
            os.close(descriptor)
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_a_read_sent_before_a_push_never_replaces_it_or_counts_as_fresh(self):
        import session_client
        env = self.env
        env.start_station_room()
        streams, subscribe = self.fake_stream()
        real = session_client.request
        with mock.patch.object(session_client, 'subscribe', side_effect=subscribe, create=True):
            window = env.launch(page=None)
            stream = streams[0]
            stream.push(env.service.execute('inspect', {}))
            env.pump(.4)

            def stale(action, arguments=None, *rest, **kw):
                answer = real(action, arguments, *rest, **kw)
                if action == 'inspect' and stale.first:
                    stale.first = False
                    time.sleep(.6)            # read now, answered after a newer push
                return answer
            stale.first = True
            with mock.patch.object(session_client, 'request', side_effect=stale):
                mark = window.polls_sent
                window.poll()
                env.pump(.1)
                env.service.execute('room_volume', {'value': 64})
                stream.push(env.service.execute('inspect', {}))
                env.pump(.2)
                self.assertEqual(round(window.volume.get_value()), 64)
                self.assertFalse(window.fresh_since(mark), 'a push counted as a fresh read')
                env.pump(1.0)
            self.assertEqual(round(window.volume.get_value()), 64, 'the older read replaced the pushed snapshot')
            self.assertTrue(window.fresh_since(mark + 1), 'no read sent after the push landed')
            window.close()
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_the_popover_mini_player_takes_pushed_snapshots_too(self):
        import next_mini
        import session_client
        env = self.env
        env.start_station_room()
        streams, subscribe = self.fake_stream()
        host = next_mini.PopoverHost(env.app, session_client.request, launch=lambda arguments: None, subscribe=subscribe)
        try:
            env.pump(.6)
            stream = streams[0]
            env.service.execute('room_volume', {'value': 52})
            stream.push(env.service.execute('inspect', {}))
            env.pump(.2)
            self.assertEqual(host.snapshot.get('room_volume'), 52)
            mark = len(env.log)
            env.pump(2.5)
            self.assertEqual(self.inspects(mark), 0, 'the popover kept polling while the stream was up')
        finally:
            host.window.close()
            env.pump(.3)
        self.assertTrue(streams[0].closed)
        env.service.execute('leave', {})
        env.settle(1.0)

    # ---- partial rebuilds and the worker handoff (WP2-WINDOW) ----------------------------------

    def counts(self, window):
        found = {}
        for prefix, owner in (('window.', window), ('rooms.', window.rooms), ('listen.', window.listen)):
            found.update({prefix + key: value for key, value in owner.rebuilds.items()})
        return found

    def polls(self, window, count=3):
        for _ in range(count):
            window.poll()
            self.env.pump(.35)

    def tagged(self, root):
        """Tag every widget under ``root``; a widget that was rebuilt comes back untagged."""
        tags = []
        for widget in self.env.widgets(root):
            if not hasattr(widget, '_smoke_tag'):
                widget._smoke_tag = next(self.tag_source)
            tags.append(widget._smoke_tag)
        return tags

    tag_source = iter(range(1, 1 << 30))

    def test_a_poll_rebuilds_only_the_section_that_changed(self):
        import o6_env
        env = self.env
        window = env.launch(page='listen')
        env.settle(.6)
        self.polls(window, 1)
        before = self.counts(window)
        shelves = {name: shown[1] for name, shown in window.listen.shelves.items() if shown[1] is not None}
        tags = {name: self.tagged(widget) for name, widget in shelves.items()}
        cards = self.tagged(window.rooms.cards)
        self.polls(window)
        self.assertEqual(self.counts(window), before, 'a poll that changed nothing drew something')
        env.service.execute('station.save', {'station': dict(o6_env.RADIO_STATION, name='Smoke partial')})
        self.polls(window)
        grown = {key: value - before.get(key, 0) for key, value in self.counts(window).items()
                 if value != before.get(key, 0)}
        self.assertEqual(grown, {'listen.shelf.stations': 1}, 'a saved station rebuilt more than Your stations')
        for name, widget in shelves.items():
            if name != 'stations':
                self.assertIs(window.listen.shelves[name][1], widget, '%s was replaced' % name)
                self.assertEqual(self.tagged(widget), tags[name], '%s was rebuilt' % name)
        self.assertEqual(self.tagged(window.rooms.cards), cards, 'the room cards were rebuilt')
        self.assertTrue(env.labels(window.listen.index_body, 'Smoke partial'), 'the new station is not shown')
        # A playing room: a volume change draws the playing-room surfaces, never the cards or the shelves.
        env.start_station_room()
        window.show_now()
        env.pump(.8)
        self.polls(window, 1)
        before = self.counts(window)
        self.polls(window)
        self.assertEqual(self.counts(window), before, 'a poll that changed nothing drew something while playing')
        env.service.execute('room_volume', {'value': 37})
        self.polls(window)
        grown = {key for key, value in self.counts(window).items() if value != before.get(key, 0)}
        self.assertEqual(grown, {'window.now', 'window.mini'})
        self.assertEqual(round(window.volume.get_value()), 37)
        env.service.execute('leave', {})
        env.settle(1.0)

    def test_slow_commands_never_delay_polls(self):
        import session_client
        env = self.env
        window = env.launch(page='rooms')
        answer = session_client.request
        release = __import__('threading').Event()

        def slow(action, arguments=None, *rest, **kw):
            if action == 'variants':
                release.wait(5)          # a busy controller: every command worker is held
            return answer(action, arguments, *rest, **kw)
        try:
            with mock.patch.object(session_client, 'request', side_effect=slow):
                for _ in range(5):
                    window.command('variants', quiet=True)
                env.pump(.2)
                mark = len(env.log)
                env.pump(1.5)
                polled = [action for action, _args in env.log[mark:] if action == 'inspect']
                self.assertGreaterEqual(len(polled), 3, 'polls waited behind slow commands')
        finally:
            release.set()
        env.pump(.5)

    def test_one_helper_hands_work_to_threads_and_answers_on_the_main_thread(self):
        import threading
        import next_async
        workers = next_async.pool('smoke-async')
        seen = []
        next_async.run_async(lambda: threading.current_thread().name, lambda name: seen.append(
            ('ok', name, threading.current_thread() is threading.main_thread())), None, workers)
        next_async.run_async(lambda: 1 / 0, None, lambda error: seen.append(
            ('err', type(error).__name__, threading.current_thread() is threading.main_thread())), workers)
        self.env.pump(.4)
        self.assertEqual(sorted((kind, value.split('_')[0], main) for kind, value, main in seen),
                         [('err', 'ZeroDivisionError', True), ('ok', 'smoke-async', True)])
        workers.shutdown()
        self.assertIsNone(next_async.run_async(lambda: 1, seen.append, None, workers))
        # Every next_* module hands work over through the helper: no private pools or done-callbacks.
        source = Path(next_async.__file__).parent
        private = [path.name for path in sorted(source.glob('next_*.py')) if path.name != 'next_async.py'
                   and any(word in path.read_text() for word in ('ThreadPoolExecutor', 'add_done_callback'))]
        self.assertEqual(private, [])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
