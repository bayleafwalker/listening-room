"""O11-CONTROL implementer tests: partial-pause status, the roomctl video verb and honest Cancel.

Everything runs on the session test double; nothing plays, downloads or opens a window.
"""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_model
import room_session as sessions
import roomctl
import session_service
import test_listening_watch

ROOM = {'name': 'Northern quiet'}


def layers(music=None, ambience=None):
    result = {}
    if music:
        result['music'] = {'status': music, 'name': 'Drone zone'}
    if ambience:
        result['atmosphere'] = {'status': ambience, 'name': 'Rain on glass', 'kind': 'local'}
    return result


class PartialPauseStatus(unittest.TestCase):
    def line(self, **kwargs):
        return roomctl.status({'active': ROOM, 'layers': layers(**kwargs)})

    def test_partial_pause_names_the_paused_layer_with_a_paused_class(self):
        line = self.line(music='Paused', ambience='Playing')
        self.assertEqual(line['text'], 'Northern quiet · Music paused · Rain on glass')
        self.assertEqual(line['class'], 'paused')
        line = self.line(music='Playing', ambience='Paused')
        self.assertEqual((line['text'], line['class']), ('Northern quiet · Ambience paused · Drone zone', 'paused'))

    def test_lines_without_partial_pause_are_the_250_ones(self):
        self.assertEqual(self.line(music='Playing', ambience='Playing')['text'], 'Northern quiet · Drone zone + Rain on glass')
        self.assertEqual(self.line(music='Playing', ambience='Playing')['class'], 'playing')
        self.assertEqual(self.line(music='Paused', ambience='Paused'), {
            'text': 'Northern quiet', 'tooltip': 'Northern quiet\nClick for room controls; right-click to browse',
            'class': 'paused', 'alt': 'room'})
        self.assertEqual(roomctl.status({})['class'], 'stopped')
        self.assertEqual(sorted(self.line(music='Paused', ambience='Playing')), ['alt', 'class', 'text', 'tooltip'])

    def test_mpris_plays_while_anything_is_audible(self):
        snapshot = {'active': ROOM, 'layers': layers(music='Paused', ambience='Playing')}
        self.assertEqual(session_service.room_status(snapshot), 'Playing')
        self.assertEqual(next_model.room_state(snapshot), 'playing')
        self.assertEqual(next_model.layer_summary(snapshot), 'Music paused · Rain on glass')

    def test_follow_prints_the_partial_pause_line(self):
        import io
        import session_client
        partial = {'active': ROOM, 'layers': layers(music='Paused', ambience='Playing')}
        subscription = mock.MagicMock()
        subscription.__iter__.return_value = iter([session_client.Update(partial, 1, 's', True, None)])
        output = io.StringIO()
        roomctl.follow_status(output, subscription)
        line = json.loads(output.getvalue())
        self.assertEqual((line['text'], line['class']), ('Northern quiet · Music paused · Rain on glass', 'paused'))


class VideoVerb(unittest.TestCase):
    def test_arguments(self):
        self.assertEqual(roomctl.video_arguments(), ('inspect', {}))
        self.assertEqual(roomctl.video_arguments('stop'), ('video', {'action': 'stop'}))
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        self.assertEqual(roomctl.video_arguments(url), ('video', {'action': 'start', 'url': url}))
        for bad in ('http://example.test/x', 'file:///etc/passwd', 'rewind', 'https://x\n', 'https://example.test/not-a-video'):
            with self.assertRaises(ValueError):
                roomctl.video_arguments(bad)

    def run_main(self, *argv, answer=None):
        printed = []
        with mock.patch.object(sys, 'argv', ['listening-roomctl', *argv]), \
                mock.patch.object(roomctl.session_client, 'request', return_value=answer) as request, \
                mock.patch('builtins.print', side_effect=lambda *a, **k: printed.append(a[0] if a else '')):
            code = roomctl.main()
        return code, request, printed

    def test_main_shows_starts_and_stops_the_video(self):
        code, request, printed = self.run_main('video', answer={'video': {'status': 'Playing', 'name': 'Rain'}})
        self.assertEqual(code, 0)
        request.assert_called_once_with('inspect', {})
        self.assertEqual(json.loads(printed[0])['name'], 'Rain')
        code, request, printed = self.run_main('video', answer={'video': None})
        self.assertEqual(printed[0], 'null')
        code, request, _ = self.run_main('video', 'https://www.youtube.com/watch?v=abcdefghijk', answer={'accepted': True})
        request.assert_called_once_with('video', {'action': 'start', 'url': 'https://www.youtube.com/watch?v=abcdefghijk'})
        code, request, _ = self.run_main('video', 'stop', answer={'message': 'Stopped watching'})
        request.assert_called_once_with('video', {'action': 'stop'})
        code, request, _ = self.run_main('cancel', answer={'cancelled': False})
        request.assert_called_once_with('entry.cancel', {})

    def test_a_link_that_is_not_a_public_youtube_video_is_refused_and_nothing_is_sent(self):
        for link in ('https://example.test/not-a-video', 'https://www.youtube.com/@somebody',
                     'https://www.youtube.com/results?search_query=rain', 'http://www.youtube.com/watch?v=abcdefghijk'):
            errors = []
            with mock.patch.object(sys, 'argv', ['listening-roomctl', 'video', link]), \
                    mock.patch.object(roomctl.session_client, 'request') as request, \
                    mock.patch('builtins.print', side_effect=lambda *a, **k: errors.append((a, k))):
                self.assertEqual(roomctl.main(), 1, link)
            request.assert_not_called()
            self.assertTrue(any('public YouTube video' in str(a[0]) and k.get('file') is sys.stderr for a, k in errors), errors)


class MediaKeys(unittest.TestCase):
    def test_the_media_key_on_the_watched_video_goes_through_the_room_transport(self):
        import listening_hub
        video = {'id': 'video', 'status': 'Playing', 'title': 'Rain', 'artist': 'Video'}
        with mock.patch.object(listening_hub, 'all_info', return_value=[video]), \
                mock.patch.object(listening_hub, 'target', return_value=video), \
                mock.patch.object(listening_hub, 'transport') as transport, \
                mock.patch('session_client.request', return_value={'active': None, 'video': {'status': 'Playing'}}), \
                mock.patch.object(sys, 'argv', ['listening-hub', 'toggle']):
            self.assertEqual(listening_hub.main(), 0)
        transport.assert_called_once_with('radio', 'play-pause')   # room transport, not the video player alone


class CancelBackendMixin:
    def blocked_start(self):
        self.backend.block = threading.Event()
        self.backend.started.clear()

    def release(self):
        self.backend.block.set()
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.backend.block = None

    def enter_room(self, room_id='library'):
        return self.service.execute('enter', {'room_id': room_id})


class HonestCancel(CancelBackendMixin, test_listening_watch.Environment):
    def setUp(self):
        super().setUp()
        self.backend = test_listening_watch.WatchBackend()
        self.now = 1000.
        self.controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        for patch in (mock.patch.object(self.controller, '_home_action'),
                      mock.patch.object(self.controller, '_route_preferred')):
            patch.start()
            self.addCleanup(patch.stop)
        self.service = session_service.Service(self.controller)
        sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'},
                   {'id': 'drone', 'name': 'Drone', 'kind': 'radio', 'url': 'https://example.test/drone'}]
        rooms = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'},
                 {'id': 'northern', 'name': 'Northern quiet', 'source_id': 'drone', 'view_id': 'room-northern'}]
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=sources),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=rooms)):
            patch.start()
            self.addCleanup(patch.stop)

    def enter(self):
        test_listening_watch.WatchController.enter(self)

    def test_entry_cancel_is_additive_and_answers_at_once(self):
        spec = sessions.ACTIONS['entry.cancel']
        self.assertTrue(spec.runs_immediately({}))
        self.assertEqual(spec.args, {'id': 'number'})
        self.assertIn('entry.cancel', session_service.MUST_RUN_IMMEDIATELY)

    def test_cancel_from_idle_starts_nothing_and_stays_idle(self):
        self.blocked_start()
        reply = self.enter_room()
        self.assertTrue(self.backend.started.wait(2))
        self.assertEqual(self.controller.snapshot()['entry']['state'], 'connecting')
        result = self.service.execute('entry.cancel', {'id': reply['id']})
        self.assertTrue(result['cancelled'])
        self.release()
        snapshot = self.controller.snapshot()
        self.assertIsNone(snapshot['active'])
        self.assertIsNone(snapshot['transition'])
        self.assertEqual(snapshot['entry']['state'], 'cancelled')
        self.assertEqual(snapshot['errors'], {})
        self.assertFalse([c for c in self.backend.calls if c[0].startswith('start')], self.backend.calls)
        self.assertEqual(snapshot['layers']['music']['status'], 'Stopped')
        self.assertEqual(next_model.start_outcome(snapshot, {'id': reply['id']}), ('started', None),
                         'a 2.5.0 window clears its Connecting card')

    def test_cancel_keeps_the_previous_room_playing_with_its_offer(self):
        self.enter()
        self.controller.layer({'layer': 'atmosphere', 'action': 'pause'})
        self.controller.quiet()
        offer = self.controller.snapshot()['resume_offer']
        self.assertIsNotNone(offer)
        self.blocked_start()
        reply = self.enter_room('northern')
        self.assertTrue(self.backend.started.wait(2))
        self.service.execute('entry.cancel', {})
        self.release()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['active']['name'], 'Midnight library')
        self.assertEqual(snapshot['resume_offer'], offer)
        self.assertEqual(snapshot['entry'], {'id': reply['id'], 'name': 'Northern quiet', 'state': 'cancelled',
                                             'kept': 'Midnight library'})
        self.assertEqual(snapshot['layers']['music']['source_id'], 'piano')
        self.assertNotIn(('start_music', 'drone'), self.backend.calls)
        self.controller.resume()
        self.assertEqual(self.controller.snapshot()['layers']['music']['status'], 'Playing')

    def test_cancel_after_the_new_player_started_stops_it_and_goes_idle(self):
        """The candidate replaced the previous music before Cancel landed: no half room, nothing new plays."""
        self.enter()
        controller, backend = self.controller, self.backend
        original = backend.start_music

        def start_then_cancel(source, cancel):
            started = original(source, cancel)
            controller.cancel_entry({})       # Cancel lands just after the new player started
            return started
        backend.start_music = start_then_cancel
        self.enter_room('northern')
        controller.worker.submit(lambda: None).result(timeout=5)
        controller.worker.submit(lambda: None).result(timeout=5)
        snapshot = controller.snapshot()
        self.assertIn(('music', 'stop', None), backend.calls)
        self.assertEqual(backend.state['music']['status'], 'Stopped')
        self.assertIsNone(snapshot['active'], 'the previous music was replaced, so the room is idle, not half')
        self.assertEqual(snapshot['entry']['state'], 'cancelled')
        self.assertIsNone(snapshot['entry']['kept'])
        self.assertEqual(controller.last_session['name'], 'Midnight library', 'Return still offers the previous room')

    def test_cancel_after_started_does_nothing(self):
        reply = self.enter_room()
        self.controller.worker.submit(lambda: None).result(timeout=5)
        result = self.service.execute('entry.cancel', {'id': reply['id']})
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['state'], 'started')
        self.assertEqual(self.controller.snapshot()['active']['name'], 'Midnight library')
        self.assertFalse(self.service.execute('entry.cancel', {})['cancelled'])

    def test_cancel_of_an_older_start_does_not_cancel_the_newer_one(self):
        self.blocked_start()
        first = self.enter_room()
        self.assertTrue(self.backend.started.wait(2))
        self.backend.block.set()
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.blocked_start()
        second = self.enter_room('northern')
        self.assertFalse(self.service.execute('entry.cancel', {'id': first['id']})['cancelled'])
        self.release()
        self.assertEqual(self.controller.snapshot()['active']['name'], 'Northern quiet')
        self.assertNotEqual(first['id'], second['id'])

    def test_failed_music_from_idle_cancelled_while_ambience_starts_leaves_nothing(self):
        controller, backend = self.controller, self.backend
        backend.fail_music = True
        original = backend.start_atmosphere

        def start_then_cancel(selection, cancel):
            result = original(selection, cancel)
            controller.cancel_entry({})
            return result
        backend.start_atmosphere = start_then_cancel
        controller.enter({'room_id': 'library', 'atmosphere': {'kind': 'radio', 'source_id': 'rain',
                                                               'url': 'https://example.test/rain'}})
        controller.worker.submit(lambda: None).result(timeout=5)
        controller.worker.submit(lambda: None).result(timeout=5)
        self.assertIsNone(controller.snapshot()['active'])
        self.assertIn(('atmosphere', 'stop', None), backend.calls)
        self.assertEqual(backend.state['atmosphere']['status'], 'Stopped')


if __name__ == '__main__':
    unittest.main()
