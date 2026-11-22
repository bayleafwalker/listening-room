"""Watch (video with sound), Show as scene and the YouTube shelves; yt-dlp and mpv are faked.

Nothing here downloads or plays anything: the controller runs on the session test double, the
shelves run a fake yt-dlp runner, and the Watch window's mpv is replaced by fixed answers.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ambience
import next_links
import next_model
import online_services
import room_session as sessions
import session_service
import youtube_search
import youtube_shelves
from test_listening_session import FakeBackend

VIDEO = 'https://www.youtube.com/watch?v=abcdefghijk'


class WatchBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.video = None
        self.fail_video = False
        self.video_block = None

    def start_video(self, item, placement='here', display=None, cancel=None):
        if self.video_block:
            self.video_block.wait(2)
        if cancel is not None and cancel.is_set():
            raise ValueError('Picture opening cancelled')
        if self.fail_video:
            raise ValueError('This video could not be played.')
        self.calls.append(('start_video', item['url'], placement))
        self.video = {'status': 'Playing', 'title': item['name'], 'ended': False}

    def stop_video(self):
        self.calls.append(('stop_video',))
        self.video = None

    def video_state(self):
        return dict(self.video) if self.video else None


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'),
            'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)


class WatchController(Environment):
    def setUp(self):
        super().setUp()
        self.backend = WatchBackend()
        self.now = 1000.
        self.controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        sources = [{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/piano'}]
        rooms = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'}]
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=sources),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=rooms),
                      mock.patch.object(self.controller, '_home_action'),
                      mock.patch.object(self.controller, '_route_preferred')):
            patch.start()
            self.addCleanup(patch.stop)
        self.service = session_service.Service(self.controller)

    def enter(self):
        self.controller.enter({'room_id': 'library', 'atmosphere': {'kind': 'radio', 'source_id': 'rain',
                                                                     'url': 'https://example.test/rain'}})
        self.controller.worker.submit(lambda: None).result(timeout=3)
        self.controller.refresh()

    def watch(self, url=VIDEO, name='Rain on a tin roof'):
        result = self.service.execute('video', {'action': 'start', 'url': url, 'name': name})
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.controller.refresh()
        return result

    def test_start_is_queued_and_stop_answers_at_once(self):
        spec = sessions.ACTIONS['video']
        self.assertFalse(spec.runs_immediately({'action': 'start'}))
        self.assertTrue(spec.runs_immediately({'action': 'stop'}))
        self.assertTrue(self.service.execute('video', {'action': 'start', 'url': VIDEO})['accepted'])

    def test_watch_pauses_music_and_ambience_like_quiet_and_keeps_the_scene(self):
        self.enter()
        self.backend.state['picture'] = {'status': 'Playing'}
        self.controller.refresh()
        self.watch()
        snapshot = self.controller.snapshot()
        self.assertIn(('music', 'pause', None), self.backend.calls)
        self.assertIn(('atmosphere', 'pause', None), self.backend.calls)
        self.assertNotIn(('picture', 'pause', None), self.backend.calls)
        self.assertEqual(snapshot['video']['status'], 'Playing')
        self.assertEqual(snapshot['video']['name'], 'Rain on a tin roof')
        self.assertEqual(snapshot['video']['url'], VIDEO)
        self.assertEqual(sorted(snapshot['resume_offer']['layers']), ['atmosphere', 'music'])
        self.assertEqual(snapshot['resume_offer']['session_id'], snapshot['active']['id'])
        # O11: the watched video is part of the room, so the room button shows Pause while it plays
        # (2.5.0 showed Play here, and Play would have resumed the room under the video).
        self.assertEqual(next_model.room_state(snapshot), 'playing')

    def test_stopping_offers_resume_and_never_resumes(self):
        self.enter()
        self.watch()
        mark = len(self.backend.calls)
        result = self.service.execute('video', {'action': 'stop'})
        self.controller.refresh()
        snapshot = self.controller.snapshot()
        self.assertTrue(result['resume_offer'])
        self.assertIsNone(snapshot['video'])
        self.assertIsNotNone(snapshot['resume_offer'])
        self.assertEqual(self.backend.calls[mark:], [('stop_video',)])
        self.assertEqual(snapshot['layers']['music']['status'], 'Paused')
        self.controller.resume()
        self.assertEqual(self.controller.snapshot()['layers']['music']['status'], 'Playing')

    def test_window_closed_or_video_ended_ends_watching_without_resuming(self):
        self.enter()
        self.watch()
        self.backend.video = None      # the listener closed the window
        self.controller.refresh()
        self.assertIsNone(self.controller.snapshot()['video'])
        self.assertIsNotNone(self.controller.snapshot()['resume_offer'])
        self.watch()
        self.backend.video['ended'] = True
        mark = len(self.backend.calls)
        self.controller.refresh()
        self.assertIsNone(self.controller.snapshot()['video'])
        self.assertIn(('stop_video',), self.backend.calls[mark:])
        self.assertNotIn(('music', 'play', None), self.backend.calls)

    def test_with_no_room_watch_just_plays(self):
        self.watch()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['video']['status'], 'Playing')
        self.assertIsNone(snapshot['resume_offer'])
        self.assertIsNone(snapshot['active'])
        self.assertEqual([call[0] for call in self.backend.calls], ['start_video'])

    def test_only_public_youtube_videos_can_be_watched(self):
        for url in ('https://radio.example.test/live.mp3', 'https://www.twitch.tv/somebody',
                    'https://www.youtube.com/@somebody', 'file:///etc/passwd', 'not a url'):
            with self.assertRaises(sessions.SessionError, msg=url):
                self.controller.dispatch('video', {'action': 'start', 'url': url})
        with self.assertRaises(sessions.SessionError):
            self.controller.dispatch('video', {'action': 'rewind'})
        self.assertEqual(self.backend.calls, [])

    def test_a_failed_video_says_why_and_keeps_the_room_paused(self):
        self.enter()
        self.backend.fail_video = True
        self.watch()
        snapshot = self.controller.snapshot()
        self.assertIsNone(snapshot['video'])
        self.assertIn('could not be played', snapshot['errors']['video'])
        self.assertIsNotNone(snapshot['resume_offer'])
        self.assertNotIn(('music', 'play', None), self.backend.calls)

    def test_stop_while_opening_cancels_it(self):
        self.enter()
        self.backend.video_block = threading.Event()
        self.service.execute('video', {'action': 'start', 'url': VIDEO})
        self.controller.stop()          # Stop invalidates the queued start
        self.backend.video_block.set()
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.assertIsNone(self.controller.snapshot()['video'])
        self.assertFalse(any(call[0] == 'start_video' for call in self.backend.calls))

    def test_leaving_the_room_forgets_the_video(self):
        self.enter()
        self.watch()
        self.controller.stop()
        self.assertIsNone(self.controller.snapshot()['video'])
        self.assertIn(('owned_video', 'stop'), self.backend.calls)

    def test_quiet_already_on_keeps_its_offer(self):
        self.enter()
        self.controller.quiet()
        offer = self.controller.snapshot()['resume_offer']
        self.watch()
        self.assertEqual(self.controller.snapshot()['resume_offer'], offer)


class VideoTransport(WatchController):
    """O11: room transport (window, MPRIS, media keys, roomctl) pauses and resumes the watched video."""

    def setUp(self):
        super().setUp()
        backend = self.backend

        def extra_video_control(action):
            backend.calls.append(('owned_video', action))
            if backend.video and action in ('pause', 'play'):
                backend.video['status'] = 'Paused' if action == 'pause' else 'Playing'
            elif action == 'stop':
                backend.video = None
        backend.extra_video_control = extra_video_control

    def transport(self, action='toggle'):
        result = self.service.execute('transport', {'action': action})
        self.controller.refresh()
        return result

    def statuses(self):
        snapshot = self.controller.snapshot()
        return (snapshot['layers']['music']['status'], snapshot['layers']['atmosphere']['status'],
                (snapshot['video'] or {}).get('status'))

    def test_transport_pauses_the_video_and_play_brings_back_only_the_video(self):
        self.enter()
        self.watch()
        self.assertEqual(self.statuses(), ('Paused', 'Paused', 'Playing'))
        self.assertTrue(self.controller.audible())
        self.assertEqual(session_service.room_status(self.controller.snapshot()), 'Playing')
        self.transport()
        self.assertEqual(self.statuses(), ('Paused', 'Paused', 'Paused'))
        self.assertTrue(self.controller.snapshot()['video']['transport_paused'])
        self.assertEqual(session_service.room_status(self.controller.snapshot()), 'Paused')
        offer = self.controller.snapshot()['resume_offer']
        self.assertEqual(offer.get('reason'), 'video', 'Quiet while watching keeps the video offer')
        self.transport()
        self.assertEqual(self.statuses(), ('Paused', 'Paused', 'Playing'))
        self.assertNotIn(('music', 'play', None), self.backend.calls)
        self.assertEqual(sorted(self.controller.snapshot()['resume_offer']['layers']), ['atmosphere', 'music'])
        # After the video, the room's own Resume is still offered and works.
        self.service.execute('video', {'action': 'stop'})
        self.controller.refresh()
        self.transport('play')
        self.assertEqual(self.statuses()[:2], ('Playing', 'Playing'))

    def test_quiet_then_stop_watching_then_resume_plays_the_room(self):
        self.enter()
        self.watch()
        self.controller.quiet()
        self.service.execute('video', {'action': 'stop'})
        self.controller.refresh()
        self.controller.resume()
        self.assertEqual(self.statuses()[:2], ('Playing', 'Playing'))

    def test_a_room_playing_beside_the_video_pauses_and_resumes_together(self):
        self.enter()
        self.watch()
        self.controller.resume()        # the listener resumed the room under the video
        self.controller.refresh()
        self.assertEqual(self.statuses(), ('Playing', 'Playing', 'Playing'))
        self.transport('pause')
        self.assertEqual(self.statuses(), ('Paused', 'Paused', 'Paused'))
        self.transport('play')
        self.assertEqual(self.statuses(), ('Playing', 'Playing', 'Playing'))

    def test_with_no_room_transport_pauses_and_plays_the_video(self):
        self.watch()
        self.transport()
        self.assertEqual(self.statuses()[2], 'Paused')
        self.assertIsNone(self.controller.snapshot()['resume_offer'])
        self.transport()
        self.assertEqual(self.statuses()[2], 'Playing')
        self.assertEqual([c for c in self.backend.calls if c[0] in ('music', 'atmosphere')], [])

    def test_a_video_paused_in_its_own_window_plays_again_from_room_play(self):
        self.enter()
        self.watch()
        self.backend.video['status'] = 'Paused'
        self.controller.refresh()
        self.assertFalse(self.controller.audible())
        self.transport()
        self.assertEqual(self.statuses(), ('Paused', 'Paused', 'Playing'))

    def test_leave_stops_the_video(self):
        self.enter()
        self.watch()
        self.controller.leave(0)
        self.assertIsNone(self.controller.snapshot()['video'])
        self.assertIn(('owned_video', 'stop'), self.backend.calls)
        self.assertIsNone(self.backend.video)

    def test_waybar_and_mpris_say_what_the_video_does(self):
        import roomctl
        self.enter()
        self.watch()
        line = roomctl.status(self.controller.snapshot())
        self.assertEqual(line['class'], 'playing')
        self.assertIn('Watching Rain on a tin roof', line['text'])
        self.assertNotIn('paused', line['text'])
        values = session_service.mpris_values(self.controller.snapshot())
        self.assertEqual((values['PlaybackStatus'], values['Metadata']['xesam:title']), ('Playing', 'Rain on a tin roof'))
        self.transport()
        line = roomctl.status(self.controller.snapshot())
        self.assertEqual((line['class'], line['text']), ('paused', 'Midnight library · Video paused'))


class ShowAsScene(unittest.TestCase):
    def snapshot(self, picture, variants=()):
        return {'active': {'id': 'library', 'room_id': 'library', 'music': {'source_id': 'piano'}, 'picture': picture},
                'variants': list(variants)}

    CATALOGUE = [{'id': 'library', 'name': 'Midnight library', 'source_id': 'piano'}]

    def test_a_linked_scene_marks_a_catalogue_room_changed(self):
        linked = {'preset_id': 'pasted-link', 'url': VIDEO}
        self.assertTrue(next_model.room_changed(self.snapshot(linked), self.CATALOGUE))
        self.assertFalse(next_model.room_changed(self.snapshot({'preset_id': 'room-library'}), self.CATALOGUE))

    def test_a_saved_room_with_the_same_linked_scene_is_unchanged(self):
        linked = {'preset_id': 'pasted-link', 'url': VIDEO}
        saved = {'id': 'library', 'name': 'Mine', 'music': {'source_id': 'piano'}, 'picture': dict(linked)}
        self.assertFalse(next_model.room_changed(self.snapshot(linked, [saved]), self.CATALOGUE))
        other = dict(saved, picture={'preset_id': 'room-library'})
        self.assertTrue(next_model.room_changed(self.snapshot(linked, [other]), self.CATALOGUE))


class LinkSheet(unittest.TestCase):
    def test_youtube_sheet_order_and_words_match_the_row(self):
        preview = online_services.preview_link(VIDEO)
        self.assertEqual([text for _key, text in next_links.sheet_destinations(preview)],
                         ['Play', 'Watch', 'Show as scene', 'Play as ambience', 'Open elsewhere'])
        # The link's own destinations (what the controller's link route accepts) are unchanged.
        self.assertNotIn('video', [key for key, _text in next_links.destinations(preview)])

    def test_other_links_are_not_offered_watch(self):
        for url in ('https://radio.example.test/live.mp3', 'https://www.twitch.tv/lofigirl',
                    'https://media.example.test/clip.mp4', 'https://www.youtube.com/@somebody'):
            preview = online_services.preview_link(url)
            self.assertEqual(next_links.sheet_destinations(preview), next_links.destinations(preview), url)


class VideoWindowState(unittest.TestCase):
    def test_ended_is_reported_for_the_watch_window(self):
        answers = {'idle-active': False, 'pause': True, 'media-title': 'Rain', 'volume': 50, 'mute': False,
                   'eof-reached': True}
        with mock.patch.object(ambience, '_ipc', side_effect=lambda command, mode='ambience', timeout=1: answers[command[1]]):
            state = ambience.state('video')
        self.assertTrue(state['ended'])
        del answers['eof-reached']

        def ipc(command, mode='ambience', timeout=1):
            if command[1] not in answers:
                raise ambience.AmbienceError('property unavailable')
            return answers[command[1]]
        with mock.patch.object(ambience, '_ipc', side_effect=ipc):
            state = ambience.state('video')
        self.assertIsNotNone(state)
        self.assertFalse(state['ended'])


def fake_runner(log, fail=(), empty=()):
    def run(argv, cancel, timeout):
        log.append(argv)
        spec = argv[-1]
        if any(word in spec for word in fail):
            raise youtube_search.SearchError('YouTube search did not finish')
        if any(word in spec for word in empty):
            return []
        live = 'results?search_query=' in spec
        return [json.dumps({'id': ('%011d' % (abs(hash(spec + str(i))) % 10 ** 11)), 'title': 'Result %d' % i,
                            'channel': 'Fixture', 'duration': None if live else 200 + i,
                            'live_status': 'is_live' if live else 'not_live'}) for i in range(3)]
    return run


ROOM = {'id': 'midnight-library', 'name': 'Midnight library', 'subtitle': 'Piano by lamplight',
        'tags': ['reading', 'dark academia', 'piano']}


class Shelves(Environment):
    def test_plan_is_derived_from_the_room_only(self):
        shelves = youtube_shelves.plan(ROOM)
        self.assertEqual([s['title'] for s in shelves], ['For Midnight library', 'Live now'])
        self.assertEqual(shelves[0]['queries'], ['reading dark academia piano music', 'Piano by lamplight ambience'])
        self.assertEqual([s['title'] for s in youtube_shelves.plan(None)], ['Live now'])

    def test_live_now_uses_the_live_filter_and_no_account(self):
        argv = youtube_shelves.live_command('live music radio')
        self.assertIn('sp=' + youtube_shelves.LIVE_FILTER, argv[-1])
        self.assertTrue(argv[-1].startswith('https://www.youtube.com/results?search_query=live+music+radio'))
        self.assertFalse(any('cookie' in a.lower() or 'username' in a.lower() or 'password' in a.lower() for a in argv))

    def test_refresh_writes_a_cache_that_is_shown_at_once_and_refreshed_hourly(self):
        log = []
        entries, failed = youtube_shelves.refresh(youtube_shelves.plan(ROOM), {}, runner=fake_runner(log), now=10_000)
        self.assertEqual(failed, [])
        self.assertEqual(len(log), 3)                   # two room queries and Live now
        self.assertTrue(all(row['live'] for row in entries['live']['rows']))
        cached = youtube_shelves.load(now=10_100)
        self.assertEqual(cached, entries)
        log.clear()
        youtube_shelves.refresh(youtube_shelves.plan(ROOM), cached, runner=fake_runner(log), now=10_000 + 1800)
        self.assertEqual(log, [])                       # not stale yet
        youtube_shelves.refresh(youtube_shelves.plan(ROOM), cached, runner=fake_runner(log), now=10_000 + 3700)
        self.assertEqual(len(log), 3)

    def test_old_entries_are_dropped_and_a_failed_refresh_keeps_the_shelf(self):
        entries, _ = youtube_shelves.refresh(youtube_shelves.plan(None), {}, runner=fake_runner([]), now=10_000)
        self.assertEqual(youtube_shelves.load(now=10_000 + 86_401), {})
        known = youtube_shelves.load(now=10_000 + 4000)
        after, failed = youtube_shelves.refresh(youtube_shelves.plan(None), known,
                                                runner=fake_runner([], fail=('results',)), now=10_000 + 4000)
        self.assertEqual(failed, ['live'])
        self.assertEqual(after['live'], entries['live'])
        after, failed = youtube_shelves.refresh(youtube_shelves.plan(None), known,
                                                runner=fake_runner([], empty=('results',)), now=10_000 + 4000)
        self.assertEqual((failed, after['live']), (['live'], entries['live']))

    def test_a_tampered_cache_cannot_inject_urls(self):
        path = youtube_shelves.cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'live': {'fetched_at': 9_000, 'rows': [
            {'id': 'abcdefghijk', 'name': 'ok', 'url': 'https://evil.example/x'},
            {'id': '../../etc/x', 'name': 'bad'}]}}))
        rows = youtube_shelves.load(now=9_100)['live']['rows']
        self.assertEqual([row['url'] for row in rows], ['https://www.youtube.com/watch?v=abcdefghijk'])

    def test_saved_youtube_comes_from_stations_and_history_without_network(self):
        station = {'id': 's1', 'name': 'Rain video', 'sources': [{'source_id': 'youtube-video', 'url': VIDEO}]}
        radio = {'id': 's2', 'name': 'Radio', 'sources': [{'source_id': 'piano', 'url': 'https://example.test/p'}]}
        found = youtube_shelves.saved_videos([radio, station], [{'station': station}])
        self.assertEqual([(row['name'], row['url']) for row in found], [('Rain video', VIDEO)])

    def test_age_words(self):
        self.assertEqual(youtube_shelves.age_words(1000, 1030), 'Updated just now')
        self.assertEqual(youtube_shelves.age_words(1000, 1000 + 300), 'Updated 5 min ago')
        self.assertEqual(youtube_shelves.age_words(1000, 1000 + 7300), 'Updated 2 h ago')


if __name__ == '__main__':
    unittest.main()
