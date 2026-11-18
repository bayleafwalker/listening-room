"""Independent acceptance oracles for WP1 (controller hot-path diet and robustness).

Owned by the acceptance author. Costs are counted by the rig in `wp1_harness.py` (fake pactl behind
subprocess.run, open/os.replace hooks, accepted connections at fake mpv sockets), not by the product.
`BASELINE_*` are the same rig's numbers on the baseline source (e306ee6), 60 fake seconds.
Behaviour that must not change (disconnect pause, call ducking) is compared with a golden log recorded from
the baseline by the same scenarios (tests/fixtures/wp1_policy_golden.json).
"""
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wp1_harness as h  # noqa: E402  (must come before any product import)
try:  # absent in the baseline tree; the negative-control run measures that tree with these oracles
    import mpv_ipc  # noqa: E402
except ImportError:
    mpv_ipc = None
import room_session  # noqa: E402
import session_service  # noqa: E402

SECONDS = 60
# Connections to the three players per minute with the baseline source: radio 5, overlay 5 and picture 3 per refresh
# (plus 4 more radio probes per refresh when a station is active).
BASELINE_CONNECTIONS = {'playing': 780, 'playing_station_active': 1020}
GOLDEN = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'wp1_policy_golden.json').read_text())['scenarios']
AUDIO_PAUSE = {'preferred_output': 'sink-a', 'pause_on_disconnect': True}


def minute(rig, volume_change=False):
    def between(second):
        if volume_change and second == 30:
            rig.players['radio'].properties['volume'] = 61.0
    rig.tick()  # warm-up refresh outside the window
    for player in rig.players.values():
        player.connections = 0
        player.commands.clear()
    rig.pactl.calls.clear()
    with h.Meter(rig.pactl) as meter:
        rig.run_seconds(SECONDS, between)
    return meter


class IdleCost(unittest.TestCase):
    def test_idle_with_policies_off_makes_no_pactl_calls_and_no_state_writes(self):
        with h.rig() as rig:
            meter = minute(rig)
            self.assertEqual(sum(rig.pactl.calls.values()), 0, dict(rig.pactl.calls))
            self.assertEqual(meter.counts['write state.json'], 0, dict(meter.counts))
            self.assertEqual([k for k in meter.counts if k.startswith('write ')], [])
            self.assertLessEqual(meter.counts['read state.json'], SECONDS, 'at most one state.json read per refresh')

    def test_idle_with_both_policies_explicitly_false_in_audio_json(self):
        with h.rig(audio_settings={'preferred_output': 'sink-a', 'pause_on_disconnect': False, 'duck_on_call': False}) as rig:
            minute(rig)
            self.assertEqual(sum(rig.pactl.calls.values()), 0, dict(rig.pactl.calls))

    def test_pause_on_disconnect_alone_lists_sinks_once_per_poll_and_nothing_else(self):
        with h.rig(audio_settings=AUDIO_PAUSE) as rig:
            minute(rig)
            self.assertEqual(dict(rig.pactl.calls), {'list sinks': SECONDS})

    def test_call_ducking_alone_lists_each_stream_kind_at_most_once_per_poll(self):
        with h.rig(audio_settings={'duck_on_call': True}, controller_settings={'duck_on_call': True}) as rig:
            minute(rig)
            self.assertLessEqual(rig.pactl.calls['list sink-inputs'], SECONDS)
            self.assertLessEqual(rig.pactl.calls['list source-outputs'], SECONDS)
            self.assertEqual(rig.pactl.calls['list sinks'], 0)
            self.assertEqual(rig.pactl.calls['get-default-sink'], 0)

    def test_both_policies_on_and_a_call_in_progress_still_lists_each_kind_at_most_once(self):
        with h.rig(audio_settings=dict(AUDIO_PAUSE, duck_on_call=True)) as rig:
            rig.pactl.sink_inputs = [h.call_stream()]
            minute(rig)
            for kind in ('list sinks', 'list sink-inputs', 'list source-outputs'):
                self.assertLessEqual(rig.pactl.calls[kind], SECONDS, kind)
            self.assertEqual(rig.pactl.calls['get-default-sink'], 0)


class PlayingCost(unittest.TestCase):
    def test_unchanged_playing_state_is_never_rewritten_and_read_once_per_refresh(self):
        with h.rig(playing=True) as rig:
            meter = minute(rig)
            self.assertEqual(meter.counts['write state.json'], 0, dict(meter.counts))
            self.assertEqual(sum(rig.pactl.calls.values()), 0)
            self.assertLessEqual(meter.counts['read state.json'], SECONDS)
            self.assertEqual(rig.controller.live['music']['status'], 'Playing')
            self.assertEqual(rig.controller.live['music']['volume'], 50.0)

    def test_a_real_volume_change_is_persisted_exactly_once_and_seen(self):
        with h.rig(playing=True) as rig:
            meter = minute(rig, volume_change=True)
            self.assertEqual(meter.counts['write state.json'], 1, dict(meter.counts))
            self.assertEqual(json.loads((h.STATE_DIR / 'state.json').read_text())['radio_audio']['volume'], 61.0)
            self.assertEqual(rig.controller.live['music']['volume'], 61.0)
            # the one refresh that persists reads state.json a second time inside update_state: bounded, not per-second
            self.assertLessEqual(meter.counts['read state.json'], SECONDS + 1)

    def test_a_mute_change_is_persisted_and_flipping_back_persists_again(self):
        with h.rig(playing=True) as rig:
            rig.tick()
            rig.players['radio'].properties['mute'] = True
            with h.Meter(rig.pactl) as meter:
                rig.tick()
                rig.tick()
                rig.players['radio'].properties['mute'] = False
                rig.tick()
                rig.tick()
            self.assertEqual(meter.counts['write state.json'], 2)
            self.assertIs(json.loads((h.STATE_DIR / 'state.json').read_text())['radio_audio']['muted'], False)

    def test_other_state_keys_survive_the_conditional_write(self):
        with h.rig(playing=True) as rig:
            state = json.loads((h.STATE_DIR / 'state.json').read_text())
            state['unrelated'] = {'keep': [1, 2, 3]}
            h.hub.save_json(h.STATE_DIR / 'state.json', state)
            rig.settle_files()
            rig.tick()
            rig.players['radio'].properties['volume'] = 33.0
            rig.tick()
            after = json.loads((h.STATE_DIR / 'state.json').read_text())
            self.assertEqual(after['unrelated'], {'keep': [1, 2, 3]})
            self.assertEqual(after['radio_audio']['volume'], 33.0)
            self.assertEqual(after['radio_socket'], h.RADIO_SOCKET)

    def test_mpv_connections_per_refresh_fall_well_below_the_baseline(self):
        with h.rig(playing=True) as rig:
            minute(rig)
            total = sum(rig.connections().values())
            self.assertLessEqual(total, .35 * BASELINE_CONNECTIONS['playing'], rig.connections())
            for name, count in rig.connections().items():
                self.assertLessEqual(count, SECONDS, (name, count))  # one connection per player per refresh
            # nothing was dropped: every property is still read
            self.assertGreaterEqual(sum(rig.commands().values()), .95 * BASELINE_CONNECTIONS['playing'])

    def test_with_a_station_active_the_extra_probes_share_connections_too(self):
        with h.rig(playing=True, active=True) as rig:
            minute(rig)
            total = sum(rig.connections().values())
            self.assertLessEqual(total, .35 * BASELINE_CONNECTIONS['playing_station_active'], rig.connections())
            self.assertLessEqual(rig.connections()['radio'], 2 * SECONDS)
            self.assertEqual(rig.players['radio'].commands['time-pos'], SECONDS)
            self.assertEqual(rig.players['radio'].commands['eof-reached'], SECONDS)

    def test_a_player_that_vanishes_is_reported_stopped_not_stale(self):
        with h.rig(playing=True) as rig:
            rig.tick()
            self.assertEqual(rig.controller.live['atmosphere']['status'], 'Playing')
            rig.players['atmosphere'].close()
            rig.tick()
            self.assertEqual(rig.controller.live['atmosphere']['status'], 'Stopped')
            rig.players['atmosphere'] = h.FakeMpv(h.RUN_DIR / 'overlay.sock', h.player_properties(20.0, 'Rain'))
            rig.tick()
            self.assertEqual(rig.controller.live['atmosphere']['status'], 'Playing')
            self.assertEqual(rig.controller.live['music']['status'], 'Playing')

    def test_radio_socket_replaced_between_refreshes_is_followed(self):
        with h.rig(playing=True) as rig:
            rig.tick()
            second = 'radio-bbbbbbbbbbbb.sock'
            rig.players['radio2'] = h.FakeMpv(h.RUN_DIR / second, h.player_properties(77.0, 'Other'))
            state = json.loads((h.STATE_DIR / 'state.json').read_text())
            state.update(radio_socket=second, radio_name='Other FM')
            h.hub.save_json(h.STATE_DIR / 'state.json', state)
            rig.tick()
            self.assertEqual(rig.controller.live['music']['name'], 'Other FM')
            self.assertEqual(rig.controller.live['music']['volume'], 77.0)
            self.assertEqual(rig.controller.live['music']['title'], 'Other')


class SnapshotCache(unittest.TestCase):
    STATION = {'name': 'Evening', 'sources': [{'source_id': 'library', 'reference': {
        'provider': 'jellyfin', 'id': 'album', 'type': 'MusicAlbum', 'name': 'Album'}}], 'end_policy': 'stop'}

    def setUp(self):
        self.rig_context = h.rig()
        self.rig = self.rig_context.__enter__()
        self.addCleanup(self.rig_context.__exit__, None, None, None)
        self.controller = self.rig.controller

    def write(self, name, value, settle=True):
        h.hub.save_json(h.STATE_DIR / name, value)
        if settle:
            self.rig.settle_files()

    def variant(self, name):
        return {'id': 'variant-' + name, 'name': name, 'music': {'source_id': 'x'}}

    def reads(self, calls=1):
        with h.Meter(self.rig.pactl) as meter:
            results = [self.controller.snapshot() for _ in range(calls)]
        return meter.counts, results

    def test_unchanged_files_cost_no_reads_across_many_snapshots(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.dispatch('station.save', {'station': self.STATION})
        self.rig.settle_files()
        self.controller.snapshot()
        counts, results = self.reads(200)
        self.assertEqual(counts['read stations.json'], 0, dict(counts))
        self.assertEqual(counts['read room-variants.json'], 0, dict(counts))
        self.assertEqual([v['name'] for v in results[-1]['variants']], ['a'])
        self.assertEqual(len(results[-1]['stations']), 1)

    def test_missing_files_cost_no_reads_and_show_empty_lists(self):
        self.controller.snapshot()
        counts, results = self.reads(50)
        self.assertEqual(counts['read stations.json'] + counts['read room-variants.json'], 0)
        self.assertEqual((results[0]['variants'], results[0]['stations']), ([], []))

    def test_file_replaced_atomically_with_same_size_inside_two_seconds_is_seen(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.snapshot()
        for letter in 'bcdefghijklmnopqrstuvwxyz':
            self.write('room-variants.json', [self.variant(letter)], settle=False)  # no ageing: same tick, same size
            self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], [letter], letter)

    def test_alternating_replacements_after_a_settled_read_are_seen(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.snapshot()
        for round_ in range(30):
            letter = 'ab'[round_ % 2] if round_ else 'z'
            self.write('room-variants.json', [self.variant(letter)], settle=False)
            self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], [letter], round_)

    def test_in_place_rewrite_with_same_size_after_a_settled_read_is_seen(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.snapshot()
        path = h.STATE_DIR / 'room-variants.json'
        path.write_text(json.dumps([self.variant('b')]))  # same inode, same size, new mtime
        self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], ['b'])

    def test_deleted_recreated_and_garbled_files(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.snapshot()
        (h.STATE_DIR / 'room-variants.json').unlink()
        self.assertEqual(self.controller.snapshot()['variants'], [])
        self.write('room-variants.json', [self.variant('a')])
        self.assertEqual(len(self.controller.snapshot()['variants']), 1)
        (h.STATE_DIR / 'room-variants.json').write_text('{ not json')
        self.assertEqual(self.controller.snapshot()['variants'], [])
        self.write('room-variants.json', [self.variant('c')])
        self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], ['c'])

    def test_stations_saved_through_the_product_appear_in_the_next_snapshot_and_survive_updates(self):
        self.controller.snapshot()
        self.controller.dispatch('station.save', {'station': self.STATION})
        first = self.controller.snapshot()['stations']
        self.assertEqual([s['name'] for s in first], ['Evening'])
        self.controller.dispatch('station.save', {'station': dict(self.STATION, name='Evenin2')})
        names = sorted(s['name'] for s in self.controller.snapshot()['stations'])
        self.assertEqual(len(names), 2, names)
        self.controller.dispatch('station.delete', {'id': first[0]['id']})
        self.assertEqual([s['name'] for s in self.controller.snapshot()['stations']], ['Evenin2'])

    def test_snapshots_do_not_share_mutable_state_with_the_cache(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.dispatch('station.save', {'station': self.STATION})
        self.rig.settle_files()
        first = self.controller.snapshot()
        first['variants'][0]['name'] = 'corrupted'
        first['variants'].append({'id': 'x'})
        first['stations'][0]['name'] = 'corrupted'
        first['stations'].clear()
        again = self.controller.snapshot()
        self.assertEqual([v['name'] for v in again['variants']], ['a'])
        self.assertEqual([s['name'] for s in again['stations']], ['Evening'])

    def test_a_file_written_in_the_future_or_by_a_skewed_clock_is_never_served_stale(self):
        self.write('room-variants.json', [self.variant('a')])
        path = h.STATE_DIR / 'room-variants.json'
        future = time.time() + 86400
        os.utime(path, (future, future))
        self.controller.snapshot()
        for letter in 'bcd':
            self.write('room-variants.json', [self.variant(letter)], settle=False)
            os.utime(path, (future + 1, future + 1))
            self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], [letter])

    def test_cache_is_per_controller_and_per_directory(self):
        self.write('room-variants.json', [self.variant('a')])
        self.controller.snapshot()
        other = room_session.Controller(backend=h.RecordingBackend(), directory=h.ROOT / 'other-dir')
        self.addCleanup(other.worker.shutdown, False)
        (h.ROOT / 'other-dir').mkdir(exist_ok=True)
        self.assertEqual(other.snapshot()['variants'], [])


class SharedConnections(unittest.TestCase):
    def setUp(self):
        self.path = h.RUN_DIR / 'unit.sock'
        self.server = h.FakeMpv(self.path, h.player_properties())
        self.addCleanup(self.server.close)

    def get(self, prop, timeout=.5):
        return mpv_ipc.call(self.path, ['get_property', prop], timeout)

    def test_plain_calls_open_one_connection_each(self):
        for _ in range(3):
            self.assertEqual(self.get('volume'), 50.0)
        self.assertEqual(self.server.connections, 3)

    def test_shared_scope_reuses_one_connection_and_closes_it_at_exit(self):
        with mpv_ipc.shared():
            values = [self.get(p) for p in ('volume', 'mute', 'pause', 'media-title')]
        self.assertEqual(values, [50.0, False, False, 'Song'])
        self.assertEqual(self.server.connections, 1)
        deadline = time.monotonic() + 2
        while self.server.open_connections and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.server.open_connections, 0, 'connection leaked past the scope')
        self.assertIsNone(getattr(mpv_ipc._local, 'pool', None))

    def test_scope_is_reset_even_when_the_body_raises(self):
        with self.assertRaises(KeyError):
            with mpv_ipc.shared():
                self.get('volume')
                raise KeyError('boom')
        self.assertIsNone(getattr(mpv_ipc._local, 'pool', None))
        self.get('volume')
        self.assertEqual(self.server.connections, 2)

    def test_pool_is_not_visible_to_other_threads(self):
        results = []
        with mpv_ipc.shared():
            self.get('volume')
            thread = threading.Thread(target=lambda: results.append((getattr(mpv_ipc._local, 'pool', None), self.get('mute'))))
            thread.start()
            thread.join(2)
        self.assertEqual(results, [(None, False)])
        self.assertEqual(self.server.connections, 2)  # the other thread used its own one-shot connection

    def test_events_interleaved_before_replies_do_not_confuse_request_matching(self):
        self.server.events_before_reply = True
        with mpv_ipc.shared():
            self.assertEqual([self.get('volume'), self.get('mute'), self.get('media-title')], [50.0, False, 'Song'])
        self.assertEqual(self.server.connections, 1)

    def test_rejected_command_keeps_the_connection_usable_and_raises_rejected(self):
        self.server.rejected.add('nope')
        with mpv_ipc.shared():
            with self.assertRaises(mpv_ipc.Rejected):
                self.get('nope')
            self.assertEqual(self.get('volume'), 50.0)
        self.assertEqual(self.server.connections, 1)

    def test_timeout_does_not_leave_a_late_reply_to_be_mistaken_for_the_next_answer(self):
        self.server.reply_delay['volume'] = .4
        with mpv_ipc.shared():
            with self.assertRaises(OSError):
                self.get('volume', timeout=.1)
            time.sleep(.5)  # the late reply is now sitting on the old connection
            self.server.reply_delay.clear()
            self.assertEqual(self.get('mute'), False)  # must be mute's answer, not volume's
            self.assertEqual(self.get('volume'), 50.0)

    def test_server_closing_mid_scope_raises_then_a_later_call_reconnects(self):
        self.server.close_after = 1
        with mpv_ipc.shared():
            self.assertEqual(self.get('volume'), 50.0)
            with self.assertRaises((mpv_ipc.Closed, OSError)):
                self.get('mute')
            self.server.close_after = None
            self.assertEqual(self.get('mute'), False)

    def test_socket_that_disappears_mid_scope_then_returns_in_a_later_scope(self):
        with mpv_ipc.shared():
            self.assertEqual(self.get('volume'), 50.0)
            self.server.close()
            with self.assertRaises((mpv_ipc.Closed, OSError)):
                self.get('mute')
            with self.assertRaises(OSError):
                self.get('pause')
        self.server = h.FakeMpv(self.path, h.player_properties(61.0))
        self.addCleanup(self.server.close)
        with mpv_ipc.shared():
            self.assertEqual(self.get('volume'), 61.0)

    def test_failed_connect_is_cached_inside_a_scope_only(self):
        missing = h.RUN_DIR / 'absent.sock'
        with mpv_ipc.shared():
            for _ in range(3):
                with self.assertRaises(OSError) as raised:
                    mpv_ipc.call(missing, ['get_property', 'volume'], .2)
            self.assertIsNotNone(raised.exception.errno)
        late = h.FakeMpv(missing, h.player_properties(9.0))
        self.addCleanup(late.close)
        with mpv_ipc.shared():
            self.assertEqual(mpv_ipc.call(missing, ['get_property', 'volume'], .2), 9.0)

    def test_nested_scopes_share_and_the_outer_one_owns_cleanup(self):
        with mpv_ipc.shared():
            self.get('volume')
            with mpv_ipc.shared():
                self.get('mute')
            self.assertIsNotNone(mpv_ipc._local.pool)  # inner exit must not tear down the outer scope
            self.get('pause')
        self.assertEqual(self.server.connections, 1)

    def test_inode_reuse_at_the_same_socket_path_inside_a_scope_is_not_served_from_the_dead_server(self):
        with mpv_ipc.shared():
            self.assertEqual(self.get('volume'), 50.0)
            self.server.close()
            replacement = h.FakeMpv(self.path, h.player_properties(88.0))
            self.addCleanup(replacement.close)
            try:
                value = self.get('volume')
            except (mpv_ipc.Closed, OSError):
                value = None
            # a stale connection to the dead server must fail; it may never answer from the old one
            self.assertIn(value, (None, 88.0))
            self.assertEqual(self.get('volume'), 88.0)


class RefreshLoop(unittest.TestCase):
    class Stop:
        """A fake threading.Event: wait() is the loop's heartbeat, and the loop ends after `ticks` of them."""
        def __init__(self, ticks, on_wait=None):
            self.ticks, self.waits, self.on_wait = ticks, 0, on_wait

        def is_set(self):
            return self.waits >= self.ticks

        def wait(self, interval):
            self.waits += 1
            if self.on_wait:
                self.on_wait(self.waits)
            return self.is_set()

    def setUp(self):
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        logger = logging.getLogger('listening-room-session')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.backend = h.RecordingBackend()
        self.rig_context = h.rig(backend=self.backend)
        self.rig = self.rig_context.__enter__()
        self.addCleanup(self.rig_context.__exit__, None, None, None)
        self.controller = self.rig.controller
        self.layer_calls = 0
        self.polls = 0
        self.plan = {}
        real_layers, real_poll = self.backend.layers, self.controller.poll_policy

        def layers():
            self.layer_calls += 1
            failure = self.plan.get(self.layer_calls)
            if failure:
                raise failure
            return real_layers()

        def poll():
            self.polls += 1
            return real_poll()
        self.backend.layers = layers
        self.controller.poll_policy = poll

    def run_loop(self, ticks, on_wait=None):
        session_service.refresh_loop(self.controller, self.Stop(ticks, on_wait), interval=0)

    def messages(self):
        return [record.getMessage() for record in self.records]

    def test_every_unexpected_exception_type_leaves_the_loop_running_and_it_recovers(self):
        failures = [AttributeError('no attr'), KeyError('missing'), TypeError('bad type'), ZeroDivisionError('zero'),
                    RuntimeError('runtime'), IndexError('index'), RecursionError('deep'), AssertionError('assert'),
                    NotImplementedError('todo'), MemoryError('mem')]
        self.plan = {i + 2: error for i, error in enumerate(failures)}
        seen = []
        self.run_loop(len(failures) + 4, lambda n: seen.append(dict(self.controller.errors)))
        self.assertEqual(self.layer_calls, len(failures) + 4, 'refresh must run on every iteration')
        self.assertEqual(self.polls, len(failures) + 4, 'policy poll must run on every iteration')
        recorded = [entry.get('refresh') for entry in seen]
        for failure, entry in zip(failures, recorded[1:]):
            self.assertIn(type(failure).__name__, entry)
        self.assertNotIn('refresh', self.controller.errors, 'a clean iteration clears the error it recorded')
        self.assertEqual(self.controller.live['music']['status'], 'Playing')

    def test_error_is_recorded_under_errors_refresh_and_visible_in_the_snapshot(self):
        self.plan = {1: KeyError('station_id'), 2: KeyError('station_id'), 3: KeyError('station_id')}
        captured = []
        self.run_loop(3, lambda n: captured.append(self.controller.snapshot()['errors'].get('refresh')))
        self.assertTrue(all('KeyError' in c and 'station_id' in c for c in captured), captured)

    def test_logs_once_per_distinct_error_with_traceback(self):
        sequence = [KeyError('a')] * 5 + [TypeError('b')] * 3 + [KeyError('a')] * 2 + [AttributeError('c')]
        self.plan = {i + 1: error for i, error in enumerate(sequence)}
        self.run_loop(len(sequence) + 1)
        messages = [m for m in self.messages() if 'refresh failed' in m]
        self.assertEqual(len(messages), 3, messages)
        self.assertTrue(all(record.exc_info for record in self.records))

    def test_a_different_error_replaces_the_recorded_one_and_recovery_clears_only_the_latest(self):
        self.plan = {1: KeyError('first'), 2: TypeError('second')}
        states = []
        self.run_loop(3, lambda n: states.append(self.controller.errors.get('refresh')))
        self.assertIn('KeyError', states[0])
        self.assertIn('TypeError', states[1])
        self.assertIsNone(states[2])

    def test_an_error_recorded_by_someone_else_is_not_cleared_by_recovery(self):
        self.controller.errors['refresh'] = 'player unreachable'
        self.run_loop(3)
        self.assertEqual(self.controller.errors.get('refresh'), 'player unreachable')

    def test_failure_late_in_refresh_after_state_was_published_does_not_stop_the_loop(self):
        calls = {'n': 0}
        original = self.backend.media_details

        def flaky_details():
            calls['n'] += 1
            if calls['n'] in (2, 3):
                raise TypeError('details exploded')
            return original()
        self.backend.media_details = flaky_details
        self.controller.active = {'id': 'room', 'music': {'source_id': 'm1', 'volume': 50}}
        self.run_loop(6)
        self.assertEqual(self.layer_calls, 6)
        self.assertEqual(calls['n'], 6)
        self.assertNotIn('refresh', self.controller.errors)

    def test_failure_in_the_audio_policy_poll_does_not_stop_refresh_and_is_logged_once(self):
        import audio_devices
        real = audio_devices.poll_policy
        count = {'n': 0}

        def boom(*args, **kwargs):
            count['n'] += 1
            if count['n'] <= 4:
                raise KeyError('policy')
            return real(*args, **kwargs)
        audio_devices.poll_policy = boom
        self.addCleanup(setattr, audio_devices, 'poll_policy', real)
        self.run_loop(7)
        self.assertEqual(self.layer_calls, 7)
        self.assertEqual(len([m for m in self.messages() if 'policy' in m]), 1, self.messages())
        self.assertNotIn('error', self.controller.policy_status.get('error') or '')  # recovered: no stale error text

    def test_refresh_and_policy_failing_together_each_iteration(self):
        import audio_devices
        real = audio_devices.poll_policy
        audio_devices.poll_policy = lambda *a, **k: (_ for _ in ()).throw(TypeError('policy'))
        self.addCleanup(setattr, audio_devices, 'poll_policy', real)
        self.plan = {i: AttributeError('layers') for i in range(1, 6)}
        self.run_loop(6)
        self.assertEqual(self.layer_calls, 6)
        self.assertEqual(self.polls, 6)

    def test_disconnect_pause_still_fires_after_the_loop_has_recovered_from_errors(self):
        rig = self.rig
        rig.controller.settings.update(pause_on_disconnect=True)
        (h.CONFIG_DIR / 'audio.json').write_text(json.dumps(AUDIO_PAUSE))
        rig.settle_files()
        rig.pactl.sinks = []
        real_run, h_subprocess = None, h.subprocess
        real_run = h_subprocess.run
        h_subprocess.run = rig.pactl.run
        self.addCleanup(setattr, h_subprocess, 'run', real_run)
        self.plan = {1: KeyError('x'), 2: KeyError('x')}
        self.run_loop(5)
        self.assertIn(('music', 'pause', None), self.backend.calls)

    def test_real_thread_survives_errors_and_stops_promptly(self):
        self.plan = {i: AttributeError('boom') for i in range(1, 6)}
        stopping = threading.Event()
        thread = threading.Thread(target=session_service.refresh_loop, args=(self.controller, stopping, .01), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while self.layer_calls < 12 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(thread.is_alive())
        self.assertGreaterEqual(self.layer_calls, 12)
        stopping.set()
        thread.join(2)
        self.assertFalse(thread.is_alive(), 'loop must exit when stopping is set')
        self.assertNotIn('refresh', self.controller.errors)

    def test_loop_never_leaks_a_shared_connection_scope_after_a_failure_inside_one(self):
        server = h.FakeMpv(h.RUN_DIR / 'overlay.sock', h.player_properties(20.0, 'Rain'))
        self.addCleanup(server.close)
        real = self.rig.controller.backend

        class Real(h.RecordingBackend):
            def layers(inner):
                with mpv_ipc.shared():
                    mpv_ipc.call(h.RUN_DIR / 'overlay.sock', ['get_property', 'volume'], .5)
                    raise TypeError('mid-scope failure')
        self.controller.backend = Real()
        self.run_loop(2)
        self.assertIsNone(getattr(mpv_ipc._local, 'pool', None))
        self.assertGreaterEqual(server.connections, 2)  # one per iteration: no reuse across iterations
        self.controller.backend = real


class PolicyBehaviourUnchanged(unittest.TestCase):
    BEHAVIOUR = ('calls', 'disconnect', 'duck', 'ducked', 'has_error', 'resume_offer')

    def test_disconnect_pause_and_call_ducking_match_the_baseline_scenario_by_scenario(self):
        self.assertEqual(set(h.POLICY_SCENARIOS), set(GOLDEN))
        for name, spec in h.POLICY_SCENARIOS.items():
            with self.subTest(scenario=name):
                log = h.run_policy_scenario(spec)
                self.assertEqual(len(log), len(GOLDEN[name]))
                for step, (got, want) in enumerate(zip(log, GOLDEN[name])):
                    for key in self.BEHAVIOUR:
                        self.assertEqual(got[key], want[key], (name, step, key, got, want))
                    policy_enabled = bool(spec.get('audio')) or bool(spec.get('controller'))
                    if policy_enabled:
                        self.assertEqual(got['call_active'], want['call_active'], (name, step))
                        self.assertEqual(got['reconnected'], want['reconnected'], (name, step))

    def test_scenarios_are_not_vacuous(self):
        log = h.run_policy_scenario(h.POLICY_SCENARIOS['pause-on-disconnect then reconnect'])
        self.assertTrue(any(('music', 'pause', None) in map(tuple, s['calls']) for s in log))
        log = h.run_policy_scenario(h.POLICY_SCENARIOS['duck on call (audio.json) and restore'])
        volumes = [c for s in log for c in s['calls'] if c[1] == 'volume']
        self.assertEqual([(c[0], c[2]) for c in volumes], [('music', 10.0), ('atmosphere', 4.0), ('music', 50.0), ('atmosphere', 20.0)])


if __name__ == '__main__':
    unittest.main()
