"""The controller's action registry and the `subscribe` push protocol.

A fake controller drives the feed for latency and cost; a real Controller with fake
players checks that dispatched changes reach subscribers. Every socket is private to
a temporary directory and nothing here plays audio.
"""
import io
import json
import os
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_session as sessions
import roomctl
import session_client
import session_service
from test_listening_session import FakeBackend


class FakeController:
    """Just enough controller for Service and the feed: a snapshot and change callbacks."""
    def __init__(self):
        self.lock = threading.RLock()
        self.state = {'active': None, 'layers': {'music': {'status': 'Stopped'}}, 'room_volume': 100.}
        self.callbacks = []
        self.dispatched = []
        self.generation = 0
        self.cancel = threading.Event()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(dict(self.state, now=time.time())))

    def change(self, **values):
        with self.lock:
            self.state.update(values)
        for callback in self.callbacks:
            callback()

    def dispatch(self, action, args):
        self.dispatched.append(action)
        if action == 'inspect':
            return dict(self.snapshot(), version=sessions.PROTOCOL_VERSION)
        return {'message': 'ok'}


def server_for(path, controller):
    service = session_service.Service(controller)
    server, descriptor = session_service.make_server(path, service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def stop():
        service.close()
        server.shutdown()
        server.server_close()
        Path(path).unlink(missing_ok=True)
        os.close(descriptor)
    return service, stop


def raw_subscribe(path, args=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2)
    connection.connect(str(path))
    connection.sendall(json.dumps({'action': 'subscribe', 'args': args or {}}).encode() + b'\n')
    return connection, connection.makefile('rb')


def wait_until(predicate, timeout=2.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def voluntary_switches(thread):
    status = Path('/proc/self/task/%d/status' % thread.native_id).read_text()
    return int(next(line.split()[1] for line in status.splitlines() if line.startswith('voluntary_ctxt_switches')))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runtime' / 'session.sock'
        self.controller = FakeController()
        self.service, stop = server_for(self.path, self.controller)
        self.addCleanup(stop)

    def subscription(self, **kwargs):
        subscription = session_client.Subscription(path=self.path, backoff=(.02, .1), **kwargs)
        self.addCleanup(subscription.close)
        updates = []
        threading.Thread(target=lambda: [updates.append((time.monotonic(), u)) for u in subscription], daemon=True).start()
        return subscription, updates


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name + '/config',
                                                   'XDG_STATE_HOME': self.tmp.name + '/state',
                                                   'XDG_CACHE_HOME': self.tmp.name + '/cache'})
        environment.start()
        self.addCleanup(environment.stop)
        self.controller = sessions.Controller(FakeBackend(), self.tmp.name)
        self.addCleanup(self.controller.close)
        self.service = session_service.Service(self.controller)

    def test_every_action_is_declared_once_and_drives_dispatch(self):
        self.assertGreaterEqual(len(sessions.ACTIONS), 48)
        for name, spec in sessions.ACTIONS.items():
            self.assertEqual(spec.name, name)
            self.assertTrue(callable(spec.handler), name)
        with self.assertRaisesRegex(RuntimeError, 'twice'):
            sessions.action('quiet')(lambda self, args: None)

    def test_the_service_queues_exactly_what_the_registry_says(self):
        immediate = sorted(name for name, spec in sessions.ACTIONS.items() if not spec.stream and spec.runs_immediately({}))
        queued = sorted(name for name, spec in sessions.ACTIONS.items() if not spec.runs_immediately({}))
        self.assertEqual(sorted(set(queued) - {'subscribe'}), ['atmosphere', 'layer', 'link', 'picture', 'save'])
        for name in ('inspect', 'get', 'enter', 'music', 'online', 'return', 'quiet', 'resume', 'stop', 'leave',
                     'wind_down', 'variants', 'delete', 'rename', 'settings', 'room_volume'):
            self.assertIn(name, immediate)
        self.assertTrue(all(name in immediate for name in sessions.ACTIONS if name.startswith(('station.', 'metadata.'))))
        for layer in ('layer', 'picture'):
            for action in ('pause', 'stop'):
                self.assertTrue(sessions.ACTIONS[layer].runs_immediately({'action': action}))
            self.assertFalse(sessions.ACTIONS[layer].runs_immediately({'action': 'volume'}))
        with mock.patch.object(self.controller.worker, 'submit') as submit:
            self.assertTrue(self.service.execute('atmosphere', {})['accepted'])
            self.assertEqual(submit.call_count, 1)
            self.service.execute('quiet', {})
            self.service.execute('layer', {'layer': 'music', 'action': 'stop'})
            self.assertEqual(submit.call_count, 1)

    def test_unknown_actions_and_malformed_arguments_fail_at_once_with_a_clear_error(self):
        with mock.patch.object(self.controller.worker, 'submit') as submit:
            for action in ('dance', 'metadata.dance', 'station.dance', '', None):
                with self.assertRaisesRegex(sessions.SessionError, 'Unknown listening session action'):
                    self.service.execute(action, {})
            submit.assert_not_called()
        for action, args, message in (('quiet', {'minutes': 'soon'}, 'quiet: minutes must be a number'),
                                      ('leave', {'fade_seconds': [1]}, 'leave: fade_seconds must be a number'),
                                      ('wind_down', {'minutes': {}}, 'wind_down: minutes must be a number'),
                                      ('enter', {'recipe': 'midnight'}, 'enter: recipe must be an object'),
                                      ('save', {'name': 'x', 'music': 3}, 'save: music must be an object'),
                                      ('subscribe', {'fields': 'active'}, 'subscribe: fields must be a list')):
            with self.assertRaisesRegex(sessions.SessionError, message):
                self.service.execute(action, args)
            with self.assertRaisesRegex(sessions.SessionError, message):
                self.controller.dispatch(action, args)
        with self.assertRaisesRegex(sessions.SessionError, 'must be an object'):
            self.controller.dispatch('quiet', ['x'])
        # Older clients that send numbers as text keep working.
        self.assertIn('message', self.service.execute('quiet', {'minutes': '0'}))

    def test_inspect_reports_the_protocol_version_and_subscribe_needs_the_socket(self):
        self.assertEqual(self.service.execute('inspect', {})['version'], sessions.PROTOCOL_VERSION)
        self.assertEqual(self.controller.dispatch('get')['version'], 2)
        self.assertNotIn('version', self.controller.snapshot())
        with self.assertRaisesRegex(sessions.SessionError, 'streams over the session socket'):
            self.service.execute('subscribe', {})


class Push(Base):
    def test_a_change_reaches_a_subscriber_in_under_100_ms(self):
        subscription, updates = self.subscription()
        self.assertTrue(wait_until(lambda: updates))
        latencies = []
        for step in range(30):
            count = len(updates)
            started = time.monotonic()
            self.controller.change(room_volume=float(step))
            self.assertTrue(wait_until(lambda: len(updates) > count, 1), step)
            received, update = updates[count]
            self.assertEqual(update.snapshot['room_volume'], float(step))
            latencies.append(received - started)
        report = {'median_ms': round(statistics.median(latencies) * 1000, 2), 'max_ms': round(max(latencies) * 1000, 2)}
        print('subscribe latency', json.dumps(report), file=sys.stderr)
        self.assertLess(max(latencies), .1, report)
        revisions = [update.revision for _, update in updates]
        self.assertEqual(revisions, sorted(set(revisions)))
        self.assertTrue(all(update.connected and update.session == self.service.feed.session for _, update in updates))

    def test_an_idle_subscriber_gets_only_heartbeats_and_its_thread_sleeps(self):
        with mock.patch.object(sessions, 'HEARTBEAT_SECONDS', .4):
            connection, stream = raw_subscribe(self.path)
            self.addCleanup(connection.close)
            first = json.loads(stream.readline())
            self.assertEqual(first['result']['event'], 'snapshot')
            self.assertEqual(first['result']['heartbeat'], .4)
            handler = next(t for t in threading.enumerate() if 'process_request' in t.name)
            before = voluntary_switches(handler)
            # Refresh ticks notify the feed with an unchanged snapshot (only `now` moves).
            for _ in range(20):
                self.controller.change()
                time.sleep(.05)
            lines = []
            connection.settimeout(.3)
            try:
                while True:
                    lines.append(json.loads(stream.readline()))
            except (socket.timeout, TimeoutError, ValueError):
                pass
            wakeups = voluntary_switches(handler) - before
        events = [line['result']['event'] for line in lines]
        self.assertTrue(events and set(events) == {'heartbeat'}, events)
        self.assertLessEqual(len(events), 4, events)
        # One wakeup per heartbeat (plus scheduling slack), never one per refresh tick.
        self.assertLessEqual(wakeups, 2 * len(events) + 2, (wakeups, events))

    def test_fields_limit_lines_to_changes_in_those_keys(self):
        subscription, updates = self.subscription(fields=['room_volume'])
        self.assertTrue(wait_until(lambda: updates))
        self.assertEqual(updates[0][1].snapshot, {'room_volume': 100.})
        self.controller.change(active={'name': 'Elsewhere'})
        time.sleep(.15)
        self.assertEqual(len(updates), 1)
        self.controller.change(room_volume=40.)
        self.assertTrue(wait_until(lambda: len(updates) == 2))
        self.assertEqual(updates[1][1].snapshot, {'room_volume': 40.})
        with self.assertRaisesRegex(session_client.SessionUnavailable, 'fields'):
            session_client._exchange(self.path, 'subscribe', {'fields': ['x' * 65]}, 1)

    def test_many_subscribers_all_see_a_change_and_the_controller_never_waits(self):
        subscribers = [self.subscription() for _ in range(40)]
        # Connecting at once can overflow the listen backlog; those clients retry.
        self.assertTrue(wait_until(lambda: all(u and u[-1][1].connected for _, u in subscribers), 5))
        self.assertEqual(len(self.service.feed._wakers), 40)
        started = time.monotonic()
        self.controller.change(room_volume=12.)
        notify_cost = time.monotonic() - started
        self.assertTrue(wait_until(lambda: all((u[-1][1].snapshot or {}).get('room_volume') == 12. for _, u in subscribers), 2))
        self.assertLess(notify_cost, .01)

    def test_a_slow_subscriber_blocks_nobody_and_is_dropped(self):
        padding = 'x' * 200_000
        with mock.patch.object(session_service, 'SEND_TIMEOUT', .5):
            stalled, _ = raw_subscribe(self.path)   # never reads
            self.addCleanup(stalled.close)
            subscription, updates = self.subscription()
            self.assertTrue(wait_until(lambda: updates and len(self.service.feed._wakers) == 2))
            worst = 0
            for step in range(15):
                started = time.monotonic()
                self.controller.change(padding=padding + str(step))
                worst = max(worst, time.monotonic() - started)
            self.assertTrue(wait_until(lambda: updates[-1][1].snapshot.get('padding', '').endswith('14'), 1))
            self.assertLess(worst, .01)
            # Once its buffer is full the stalled subscriber's next send times out on
            # its own thread, and it gives up its slot.
            def dropped():
                self.controller.change(padding=padding + str(time.monotonic()))
                time.sleep(.05)
                return len(self.service.feed._wakers) == 1
            self.assertTrue(wait_until(dropped, 3))
            started = time.monotonic()
            self.controller.change(room_volume=1.)
            self.assertTrue(wait_until(lambda: updates[-1][1].snapshot['room_volume'] == 1., .1))

    def test_a_subscriber_that_disconnects_mid_stream_is_released_at_once(self):
        subscription, updates = self.subscription()
        connection, stream = raw_subscribe(self.path)
        stream.readline()
        self.assertTrue(wait_until(lambda: updates and len(self.service.feed._wakers) == 2))
        stream.close()
        connection.close()
        # Released on hang-up, not at the next heartbeat (15 s).
        self.assertTrue(wait_until(lambda: len(self.service.feed._wakers) == 1, .5))
        self.controller.change(room_volume=3.)
        self.assertTrue(wait_until(lambda: updates[-1][1].snapshot['room_volume'] == 3., .1))

    def test_subscribers_are_bounded_and_hardened_like_requests(self):
        self.service.feed.limit = 2
        held = [raw_subscribe(self.path) for _ in range(2)]
        for connection, stream in held:
            self.addCleanup(connection.close)
            stream.readline()
        with self.assertRaisesRegex(session_client.SessionUnavailable, 'Too many listening subscribers'):
            session_client._exchange(self.path, 'subscribe', {}, 1)
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.path))
            payload = b'{"action":"subscribe","args":{"fields":["' + b'a' * session_client.MAX_MESSAGE + b'"]}}\n'
            def send():
                try:
                    connection.sendall(payload)
                except OSError:   # the server stops reading at the limit
                    pass
            threading.Thread(target=send, daemon=True).start()
            response = json.loads(connection.makefile('rb').readline())
        self.assertFalse(response['ok'])
        self.assertIn('too large', response['error'])
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(session_client.SessionUnavailable, 'another user'):
                session_client._exchange(self.path, 'subscribe', {}, 1)

    def test_queued_and_immediate_actions_without_callbacks_still_publish(self):
        subscription, updates = self.subscription()
        self.assertTrue(wait_until(lambda: updates))
        with self.controller.lock:
            self.controller.state['room_volume'] = 55.   # a handler that changes state silently
        self.service.execute('variants', {})
        self.assertTrue(wait_until(lambda: updates[-1][1].snapshot['room_volume'] == 55., .1))


class ReviewFixes(Base):
    """Defects found by the independent review of the push path."""
    def read_event(self, reader):
        return json.loads(reader.readline())['result']

    def test_refresh_errors_and_policy_changes_reach_subscribers(self):
        controller = self.controller
        controller.errors, controller.policy_status = {}, {}
        controller.refresh_failure = RuntimeError('backend down')
        controller.snapshot = lambda: json.loads(json.dumps(dict(
            controller.state, errors=dict(controller.errors), audio_policy=controller.policy_status, now=time.time())))
        controller._changed = lambda: [callback() for callback in controller.callbacks]
        def refresh():
            if controller.refresh_failure:
                raise controller.refresh_failure
        controller.refresh = refresh
        policy = [{'duck': False}]
        controller.poll_policy = lambda: setattr(controller, 'policy_status', policy[0]) or controller._changed()
        stopping = threading.Event()
        self.addCleanup(stopping.set)
        connection, reader = raw_subscribe(self.path, {'fields': ['errors', 'audio_policy']})
        self.addCleanup(connection.close)
        self.assertEqual(self.read_event(reader)['snapshot']['errors'], {})
        threading.Thread(target=session_service.refresh_loop, args=(controller, stopping, .05), daemon=True).start()
        seen = self.read_event(reader)['snapshot']
        while 'refresh' not in seen['errors']:
            seen = self.read_event(reader)['snapshot']
        self.assertIn('backend down', seen['errors']['refresh'])
        controller.refresh_failure = None
        while seen['errors']:
            seen = self.read_event(reader)['snapshot']
        # The loop's own policy-poll failure is published too.
        def failing_poll():
            raise OSError('no pactl')
        controller.poll_policy = failing_poll
        while seen['audio_policy'] != {'error': 'no pactl'}:
            seen = self.read_event(reader)['snapshot']

    def test_poll_policy_publishes_a_changed_status(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': directory + '/config', 'XDG_STATE_HOME': directory + '/state',
                                             'XDG_CACHE_HOME': directory + '/cache'}):
            controller = sessions.Controller(FakeBackend(), directory)
            self.addCleanup(controller.close)
            calls = []
            controller.callbacks.append(lambda: calls.append(1))
            fake_audio = mock.Mock(poll_policy=mock.Mock(return_value={'duck': False, 'sinks': 2}))
            with mock.patch.dict(sys.modules, {'audio_devices': fake_audio}):
                controller.poll_policy()
                controller.poll_policy()
            self.assertEqual(len(calls), 1)   # once for the change, none for the repeat
            self.assertEqual(controller.snapshot()['audio_policy'], {'duck': False, 'sinks': 2})

    def test_a_failing_snapshot_never_fails_a_subscribe(self):
        self.service.feed   # a first good revision exists
        for failure in (sessions.SessionError('transient'), RuntimeError('boom')):
            self.controller.snapshot_failure = failure
            original = self.controller.snapshot
            def failing(original=original):
                if self.controller.snapshot_failure:
                    raise self.controller.snapshot_failure
                return original()
            self.controller.snapshot = failing
            self.addCleanup(setattr, self.controller, 'snapshot', original)
            good = self.service.feed.latest()[0]
            connection, reader = raw_subscribe(self.path)
            self.addCleanup(connection.close)
            event = self.read_event(reader)
            self.assertEqual(event['event'], 'snapshot')
            self.assertEqual(event['revision'], good)   # the latest good revision
            self.controller.snapshot_failure = None
            self.controller.snapshot = original

    def test_a_subscribe_before_any_snapshot_waits_for_the_first_one(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = FakeController()
            original = controller.snapshot
            controller.snapshot = mock.Mock(side_effect=sessions.SessionError('not yet'))
            path = Path(directory) / 'runtime' / 'session.sock'
            service, stop = server_for(path, controller)
            self.addCleanup(stop)
            connection, reader = raw_subscribe(path)
            self.addCleanup(connection.close)
            time.sleep(.2)
            controller.snapshot = original
            controller.change(room_volume=50.)
            self.assertEqual(self.read_event(reader)['snapshot']['room_volume'], 50.)

    def test_a_subscribers_first_snapshot_includes_a_change_made_just_before(self):
        connection, reader = raw_subscribe(self.path)
        self.addCleanup(connection.close)
        self.read_event(reader)
        self.controller.state['room_volume'] = 12.   # acknowledged, callback not yet delivered
        late, late_reader = raw_subscribe(self.path)
        self.addCleanup(late.close)
        self.assertEqual(self.read_event(late_reader)['snapshot']['room_volume'], 12.)

    def test_listeners_run_only_on_the_feed_thread_and_may_refresh(self):
        feed = self.service.feed
        threads, outcome = set(), []
        def listener(revision, snapshot):
            threads.add(threading.current_thread().name)
            outcome.append(feed.refresh())   # would deadlock if run inside a publish
        feed.add_listener(listener)
        subscribers = [raw_subscribe(self.path) for _ in range(3)]
        for connection, _ in subscribers:
            self.addCleanup(connection.close)
        self.controller.state['room_volume'] = 7.   # visible only to a refreshing subscriber
        late, late_reader = raw_subscribe(self.path)
        self.addCleanup(late.close)
        self.assertEqual(self.read_event(late_reader)['snapshot']['room_volume'], 7.)
        self.controller.change(room_volume=8.)
        self.assertTrue(wait_until(lambda: len(outcome) >= 2))
        self.assertEqual(threads, {'room-feed'})

    def test_a_listener_refresh_does_not_block_the_feed_thread(self):
        feed = self.service.feed
        feed.add_listener(lambda *_: feed.refresh())
        started = time.monotonic()
        self.controller.change(room_volume=3.)
        self.assertTrue(wait_until(lambda: feed.latest()[1]['room_volume'] == 3.))
        self.assertLess(time.monotonic() - started, 1.)

    def test_the_version_probe_is_cheap_under_a_held_lock_and_for_an_oversize_snapshot(self):
        class Probed(FakeController):
            dispatch = sessions.Controller.dispatch
        controller = Probed()
        controller.state['blob'] = 'x' * (session_client.MAX_MESSAGE + 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime' / 'session.sock'
            service, stop = server_for(path, controller)
            self.addCleanup(stop)
            with controller.lock:
                held = threading.Event()
                result = []
                def probe():
                    held.set()
                    result.append(session_client._exchange(path, 'inspect', {'fields': []}, 1))
                thread = threading.Thread(target=probe)
                thread.start()
                held.wait()
                thread.join(2)   # the controller lock is this thread's: the probe must not need it
                self.assertEqual(result, [{'version': sessions.PROTOCOL_VERSION}])
            updates = []
            subscription = session_client.subscribe(updates.append, path=path, fields=['room_volume'], backoff=(.02, .1))
            self.addCleanup(subscription.close)
            self.assertTrue(wait_until(lambda: updates and updates[0].connected, 4))

    def test_the_probe_timeout_survives_a_slow_controller(self):
        self.assertGreaterEqual(session_client.PROBE_TIMEOUT, 5)

    def test_an_oversize_protocol_1_snapshot_is_reported_as_an_older_controller(self):
        class Old(FakeController):
            def dispatch(self, action, args):
                return {'blob': 'x' * (session_client.MAX_MESSAGE + 10)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime' / 'session.sock'
            service, stop = server_for(path, Old())
            self.addCleanup(stop)
            update = next(iter(session_client.Subscription(path=path)))
            self.assertIn('older than this client', update.error)


class Reconnect(unittest.TestCase):
    def test_the_client_reports_outages_once_and_resumes_after_a_restart_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime' / 'session.sock'
            first = FakeController()
            service, stop = server_for(path, first)
            sent = []
            original = session_client._exchange
            def recording(target, action, args, timeout):
                sent.append(action)
                return original(target, action, args, timeout)
            updates = []
            with mock.patch.object(session_client, '_exchange', recording), \
                    mock.patch.object(session_client, 'ensure_service', side_effect=AssertionError('started')), \
                    mock.patch('subprocess.run', side_effect=AssertionError('started')), \
                    mock.patch('subprocess.Popen', side_effect=AssertionError('started')):
                subscription = session_client.subscribe(updates.append, path=path, backoff=(.02, .05))
                try:
                    self.assertTrue(wait_until(lambda: updates))
                    old_session = updates[0].session
                    stop()
                    self.assertTrue(wait_until(lambda: not updates[-1].connected))
                    time.sleep(.3)   # several retries while the controller is gone
                    self.assertEqual(sum(1 for update in updates if not update.connected), 1)
                    second = FakeController()
                    second.state['room_volume'] = 20.
                    service, stop = server_for(path, second)
                    self.assertTrue(wait_until(lambda: updates[-1].connected, 3))
                    self.assertNotEqual(updates[-1].session, old_session)
                    self.assertEqual(updates[-1].snapshot['room_volume'], 20.)
                finally:
                    subscription.close()
                    stop()
            self.assertEqual(set(sent), {'inspect'})
            self.assertEqual(set(first.dispatched) | set(second.dispatched), {'inspect'})

    def test_an_older_controller_is_never_sent_subscribe(self):
        class Old(FakeController):
            def dispatch(self, action, args):
                self.dispatched.append(action)
                return self.snapshot()   # protocol 1: no version
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime' / 'session.sock'
            controller = Old()
            service, stop = server_for(path, controller)
            self.addCleanup(stop)
            with mock.patch.object(session_service.RequestHandler, 'subscribe', side_effect=AssertionError('subscribed')):
                update = next(iter(session_client.Subscription(path=path)))
            self.assertFalse(update.connected)
            self.assertIn('older than this client', update.error)
            self.assertEqual(controller.dispatched, ['inspect'])

    def test_close_ends_a_blocked_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime' / 'session.sock'
            service, stop = server_for(path, FakeController())
            self.addCleanup(stop)
            subscription = session_client.Subscription(path=path)
            done = threading.Event()
            threading.Thread(target=lambda: (list(subscription), done.set()), daemon=True).start()
            self.assertTrue(wait_until(lambda: len(service.feed._wakers) == 1))
            subscription.close()
            self.assertTrue(done.wait(1))


class RealController(unittest.TestCase):
    def test_dispatched_changes_reach_a_subscriber(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': directory + '/config', 'XDG_STATE_HOME': directory + '/state',
                                             'XDG_CACHE_HOME': directory + '/cache'}):
            controller = sessions.Controller(FakeBackend(), directory)
            self.addCleanup(controller.close)
            path = Path(directory) / 'runtime' / 'session.sock'
            service, stop = server_for(path, controller)
            self.addCleanup(stop)
            updates = []
            subscription = session_client.subscribe(updates.append, path=path, fields=['room_volume', 'errors'])
            self.addCleanup(subscription.close)
            self.assertTrue(wait_until(lambda: updates))
            self.assertEqual(updates[0].snapshot['errors'], {})
            session_client._exchange(path, 'room_volume', {'value': 30}, 1)
            self.assertTrue(wait_until(lambda: updates[-1].snapshot['room_volume'] == 30., .2))
            # A queued action runs on the worker; its outcome (here a refusal) is pushed too.
            self.assertTrue(session_client._exchange(path, 'save', {'name': 'Pushed room'}, 1)['accepted'])
            self.assertTrue(wait_until(lambda: 'Enter a room' in updates[-1].snapshot['errors'].get('session', '')))
            self.assertEqual(controller.backend.calls, [])   # nothing started playing


class FollowStatus(unittest.TestCase):
    def test_status_follow_prints_a_line_per_visible_change_and_stopped_when_offline(self):
        playing = {'active': {'name': 'Library'}, 'layers': {'music': {'status': 'Playing', 'name': 'Piano'}}}
        stream = [session_client.Update(None, None, None, False, 'not running'),
                  session_client.Update(playing, 1, 's', True, None),
                  session_client.Update(dict(playing, room_volume=10), 2, 's', True, None),
                  session_client.Update(None, None, None, False, 'gone')]
        subscription = mock.MagicMock()
        subscription.__iter__.return_value = iter(stream)
        output = io.StringIO()
        self.assertEqual(roomctl.follow_status(output, subscription), 0)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line['class'] for line in lines], ['stopped', 'playing', 'stopped'])
        self.assertEqual(lines[1]['text'], 'Library · Piano')
        subscription.close.assert_called_once()

    def test_status_follow_stops_quietly_when_waybar_closes_the_pipe(self):
        output = mock.Mock()
        output.write.side_effect = BrokenPipeError
        subscription = mock.MagicMock()
        subscription.__iter__.return_value = iter([session_client.Update({}, 1, 's', True, None)])
        self.assertEqual(roomctl.follow_status(output, subscription), 0)

    def test_status_follow_exits_when_its_reader_is_gone_even_without_a_status_change(self):
        read, write = os.pipe()
        output = os.fdopen(write, 'w')
        self.addCleanup(lambda: output.closed or output.close())
        released = threading.Event()
        class Idle:
            """A subscription that reports once, then idles until closed."""
            def __iter__(self):
                yield session_client.Update({}, 1, 's', True, None)
                released.wait(10)
            def close(self):
                released.set()
        finished = []
        thread = threading.Thread(target=lambda: finished.append(roomctl.follow_status(output, Idle())), daemon=True)
        thread.start()
        self.assertEqual(os.read(read, 1000)[:1], b'{')
        time.sleep(.1)
        self.assertTrue(thread.is_alive())   # a live reader keeps it following
        os.close(read)
        thread.join(2)
        self.assertEqual(finished, [0])
        self.assertTrue(released.is_set())

    def test_follow_requires_status_and_status_never_starts_the_controller(self):
        with mock.patch.object(sys, 'argv', ['listening-roomctl', 'inspect', '--follow']), \
                mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            roomctl.main()
        with mock.patch.object(sys, 'argv', ['listening-roomctl', 'status', '--follow']), \
                mock.patch.object(roomctl, 'follow_status', return_value=0) as follow:
            self.assertEqual(roomctl.main(), 0)
        follow.assert_called_once_with()
        with mock.patch.object(session_client, 'Subscription') as subscription:
            subscription.return_value.__iter__.return_value = iter([])
            roomctl.follow_status(io.StringIO())
        self.assertFalse(subscription.call_args.kwargs.get('start', False))


if __name__ == '__main__':
    unittest.main()
