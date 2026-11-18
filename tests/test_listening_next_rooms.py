"""Rooms for the next window: filters, Surprise me, saved rooms, legacy scenes, the preview words and rename."""
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_rooms_model as rooms
import room_catalogue
import room_session as sessions
import session_service
from test_listening_session import FakeBackend

FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh', 'epoch')


def catalogue():
    result = []
    for recipe in room_catalogue.RECIPES:
        item = {key: value for key, value in recipe.items() if key != 'view_ids'}
        item.update(source_name=recipe['name'] + ' radio', source_kind='web' if recipe['id'] == 'finnish-discoveries' else 'radio',
                    view_id='view-' + recipe['id'], view_name='View of ' + recipe['name'], poster_path='/art/%s.jpg' % recipe['id'],
                    accent='#c79b68', available=True)
        result.append(item)
    return result


def variant(name='Rainy reading', **changes):
    value = {'id': rooms.variant_id(name), 'name': name, 'room_id': 'midnight-library', 'poster_path': '/art/midnight-library.jpg',
             'music': {'source_id': 'dark-academia-piano', 'name': 'Piano'},
             'atmosphere': {'kind': 'local', 'name': 'Rain on glass', 'volume': 30},
             'picture': {'preset_id': 'view-midnight-library', 'enabled': True, 'placement': '9'}}
    value.update(changes)
    return value


def snapshot(**changes):
    value = {'active': None, 'last_session': None, 'variants': [], 'recent': [], 'settings': {}, 'now': 1000., 'room_volume': 80,
             'layers': {}, 'errors': {}, 'levels': {}}
    value.update(changes)
    return value


def names(items):
    return [item['name'] for item in items]


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = catalogue()
        self.entries = rooms.entries(snapshot(), self.catalogue)

    def test_every_chip_has_the_words_on_the_screen(self):
        self.assertEqual([text for _key, text in rooms.MOODS],
                         ['All', 'Yours', 'Focus', 'Reading', 'Late night', 'Lively', 'Something new'])

    def test_all_shows_every_available_room(self):
        self.assertEqual(len(rooms.filtered(self.entries, 'all')), 8)
        gone = [dict(room, available=False) if room['id'] == 'night-drive' else room for room in self.catalogue]
        self.assertEqual(len(rooms.entries(snapshot(), gone)), 7)

    def test_mood_chips_use_authored_traits_and_tags(self):
        late = names(rooms.filtered(self.entries, 'late'))
        self.assertIn('Night drive', late)
        self.assertIn('After hours', late)
        self.assertNotIn('Northern quiet', late)
        self.assertIn('Northern quiet', names(rooms.filtered(self.entries, 'focus')))
        self.assertIn('Midnight library', names(rooms.filtered(self.entries, 'reading')))
        self.assertIn('Velvet circuit', names(rooms.filtered(self.entries, 'lively')))
        added = dict(self.catalogue[0], id='new-room', name='New room', tags=['ambient'])
        self.assertIn('New room', names(rooms.filtered(rooms.entries(snapshot(), [added]), 'focus')))

    def test_something_new_is_authored_then_follows_history(self):
        fresh = names(rooms.filtered(self.entries, 'new', snapshot()))
        self.assertEqual(fresh, ['Finnish discoveries'])
        seen = snapshot(recent=['midnight-library', 'night-drive'], last_session={'id': 'midnight-library'})
        unseen = names(rooms.filtered(rooms.entries(seen, self.catalogue), 'new', seen))
        self.assertIn('Finnish discoveries', unseen)
        self.assertIn('Northern quiet', unseen)
        self.assertNotIn('Night drive', unseen)
        self.assertNotIn('Midnight library', unseen)

    def test_yours_lists_only_saved_rooms_and_they_come_first(self):
        shown = snapshot(variants=[variant()])
        items = rooms.entries(shown, self.catalogue)
        self.assertEqual(items[0]['name'], 'Rainy reading')
        self.assertTrue(items[0]['yours'])
        self.assertEqual(names(rooms.filtered(items, 'yours', shown)), ['Rainy reading'])
        self.assertEqual(items[0]['sub'], 'Midnight library, with Rain on glass')
        self.assertIn('Rainy reading', names(rooms.filtered(items, 'reading', shown)))
        self.assertNotIn('Rainy reading', names(rooms.filtered(items, 'new', shown)))


class SurpriseTests(unittest.TestCase):
    def setUp(self):
        self.entries = rooms.entries(snapshot(), catalogue())

    def test_surprise_respects_the_chip(self):
        for seed in range(20):
            pick = rooms.surprise(self.entries, 'late', snapshot(), random.Random(seed))
            self.assertIn('late', pick['moods'])

    def test_surprise_skips_the_room_you_are_in_and_just_left(self):
        shown = snapshot(active={'id': 'night-drive', 'room_id': 'night-drive', 'name': 'Night drive'},
                         recent=['night-drive', 'after-hours'])
        for seed in range(30):
            pick = rooms.surprise(self.entries, 'late', shown, random.Random(seed))
            self.assertNotIn(pick['id'], ('night-drive', 'after-hours'))

    def test_surprise_relaxes_history_before_giving_up(self):
        only = [item for item in self.entries if item['id'] in ('night-drive', 'after-hours')]
        shown = snapshot(active={'id': 'night-drive'}, recent=['after-hours', 'night-drive'])
        self.assertEqual(rooms.surprise(only, 'all', shown)['id'], 'after-hours')
        alone = [item for item in self.entries if item['id'] == 'night-drive']
        self.assertEqual(rooms.surprise(alone, 'all', shown)['id'], 'night-drive')
        self.assertIsNone(rooms.surprise(alone, 'yours', shown))


class EnterAndSwitchTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = catalogue()

    def test_catalogue_rooms_enter_with_their_scene_and_saved_rooms_keep_theirs(self):
        items = {item['id']: item for item in rooms.entries(snapshot(variants=[variant()]), self.catalogue)}
        self.assertEqual(rooms.enter_args(items['night-drive']), {'room_id': 'night-drive', 'with_view': True})
        self.assertEqual(rooms.enter_args(items['night-drive'], False), {'room_id': 'night-drive', 'with_view': False})
        saved = items[rooms.variant_id('Rainy reading')]
        self.assertEqual(rooms.enter_args(saved), {'variant_id': saved['id']})

    def test_back_to_the_previous_room_goes_to_a_saved_room_or_a_catalogue_room(self):
        saved = variant()
        shown = snapshot(variants=[saved], active=dict(saved))
        self.assertEqual(rooms.previous_room(shown, self.catalogue), {'name': 'Rainy reading', 'args': {'variant_id': saved['id']}})
        catalogue_room = snapshot(active={'id': 'night-drive', 'room_id': 'night-drive', 'name': 'Night drive'})
        self.assertEqual(rooms.previous_room(catalogue_room, self.catalogue)['args'], {'room_id': 'night-drive'})
        custom = snapshot(active={'id': 'custom', 'room_id': None, 'name': 'Your listening room'})
        self.assertEqual(rooms.previous_room(custom, self.catalogue)['args'], {'recipe': custom['active']})
        self.assertIsNone(rooms.previous_room(snapshot(), self.catalogue))

    def test_a_saved_room_based_on_the_playing_room_does_not_mark_it_playing(self):
        saved = variant()
        shown = snapshot(variants=[saved], active=dict(saved))
        items = {item['id']: item for item in rooms.entries(shown, self.catalogue)}
        self.assertTrue(rooms.is_active(items[saved['id']], shown))
        self.assertFalse(rooms.is_active(items['midnight-library'], shown))


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = catalogue()
        self.items = {item['id']: item for item in rooms.entries(snapshot(variants=[variant()]), self.catalogue)}

    def test_live_radio_says_programming_varies_and_the_scene_has_a_toggle(self):
        view = rooms.preview(self.items['night-drive'], snapshot())
        self.assertEqual(view['music_detail'], 'Live radio · programming varies')
        self.assertEqual(view['scene'], 'Scene on workspace 9 · moving')
        self.assertTrue(view['scene_available'] and view['scene_on'])
        self.assertEqual(view['ambience'], 'No ambience')
        self.assertEqual(view['button'], 'Enter room')
        self.assertEqual(view['tags'], ['Driving', 'Presenters', 'Landscapes'])

    def test_scene_wording_follows_the_placement_motion_and_toggle(self):
        entry = self.items['night-drive']
        self.assertEqual(rooms.preview(entry, snapshot(settings={'reduced_motion': True}))['scene'], 'Scene on workspace 9 · still')
        self.assertEqual(rooms.preview(entry, snapshot(settings={'placement': 'here'}))['scene'], 'Scene here · moving')
        self.assertEqual(rooms.preview(dict(entry, scene_on=False), snapshot())['scene'], 'Scene off · sound only')
        self.assertEqual(rooms.preview(dict(entry, scene_available=False, scene_on=False), snapshot())['scene'], 'No scene for this room')

    def test_the_note_says_what_happens_to_the_current_room(self):
        entry = self.items['night-drive']
        shown = snapshot(active={'id': 'midnight-library', 'name': 'Midnight library'})
        self.assertEqual(rooms.preview(entry, shown)['note'], 'Midnight library fades out first. Other apps keep playing.')
        self.assertIn('Other apps keep playing', rooms.preview(entry, snapshot())['note'])
        here = snapshot(active={'id': 'night-drive', 'name': 'Night drive'})
        view = rooms.preview(entry, here)
        self.assertEqual((view['button'], view['inside']), ('Open room', True))

    def test_saved_rooms_preview_their_own_ambience(self):
        saved = self.items[rooms.variant_id('Rainy reading')]
        view = rooms.preview(saved, snapshot())
        self.assertEqual(view['ambience'], 'Rain on glass')
        self.assertEqual(view['title'], 'Rainy reading')

    def test_no_internal_vocabulary(self):
        shown = snapshot(active={'id': 'midnight-library', 'name': 'Midnight library'}, settings={'reduced_motion': True})
        for entry in self.items.values():
            view = rooms.preview(entry, shown)
            for value in view.values():
                for text in (value if isinstance(value, list) else [value]):
                    if isinstance(text, str):
                        for word in FORBIDDEN:
                            self.assertNotIn(word, text)
                        if 'Workspace 9' in text:
                            self.fail('Workspace 9 belongs to the Mix Scene row: %r' % text)

    def test_save_lines_list_what_will_be_kept(self):
        shown = snapshot(active={'id': 'midnight-library', 'name': 'Midnight library',
                                 'music': {'source_id': 'p', 'name': 'Piano'}, 'atmosphere': {'kind': 'local', 'name': 'Rain on glass'},
                                 'picture': {'preset_id': 'x', 'enabled': True, 'placement': '9'}},
                         layers={'music': {'status': 'Playing', 'station': 'Piano'},
                                 'atmosphere': {'status': 'Playing', 'kind': 'local', 'name': 'Rain on glass'},
                                 'picture': {'status': 'Playing', 'name': 'Scene', 'placement': '9'}})
        self.assertEqual(rooms.save_lines(shown), [('Music', 'Piano'), ('Ambience', 'Rain on glass'),
                                                   ('Scene', 'on workspace 9 · moving'), ('Room volume', '80%')])


class LegacySceneTests(unittest.TestCase):
    """Local state has no classic scenes today, so the migration is covered with a fixture."""
    def setUp(self):
        self.catalogue = catalogue()
        self.sources = [{'id': 'truckersfm', 'name': 'TruckersFM', 'kind': 'radio'}]
        self.presets = [{'id': 'northern-lake', 'name': 'Northern lake', 'poster_path': '/art/lake.jpg', 'accent': '#88aacc'}]
        self.scenes = [{'id': 'dark-academia', 'name': 'Dark academia', 'source_id': 'truckersfm'},  # built in
                       {'id': 'user-1', 'name': 'Road trip', 'source_id': 'truckersfm', 'volume': 35, 'ambience_id': 'northern-lake',
                        'placement': 'here'},
                       {'id': 'user-2', 'name': 'Gone', 'source_id': 'missing-station'}]

    def test_saved_scenes_appear_under_yours_and_enter_as_a_recipe(self):
        legacy = rooms.legacy_scenes(self.scenes, self.sources, self.presets, self.catalogue)
        self.assertEqual(names(legacy), ['Road trip'])
        entry = legacy[0]
        self.assertTrue(entry['yours'] and entry['legacy'] and not entry['editable'])
        recipe = entry['args']['recipe']
        self.assertEqual(recipe['music'], {'source_id': 'truckersfm', 'name': 'TruckersFM', 'volume': 35, 'muted': False})
        self.assertEqual(recipe['picture']['placement'], 'here')
        self.assertTrue(recipe['picture']['enabled'])
        self.assertEqual(entry['sub'], 'TruckersFM · from your scenes')

    def test_scenes_follow_saved_rooms_and_never_duplicate_a_name(self):
        legacy = rooms.legacy_scenes(self.scenes, self.sources, self.presets, self.catalogue)
        shown = snapshot(variants=[variant('Another room'), variant('road trip')])
        items = rooms.entries(shown, self.catalogue, self.sources, legacy)
        self.assertEqual(names(items)[:2], ['Another room', 'road trip'])
        self.assertNotIn('Road trip', names(items))
        shown = snapshot(variants=[variant('Another room')])
        items = rooms.entries(shown, self.catalogue, self.sources, legacy)
        self.assertEqual(names(items)[:2], ['Another room', 'Road trip'])
        self.assertEqual(len(rooms.filtered(items, 'yours', shown)), 2)

    def test_a_scene_without_a_view_enters_sound_only(self):
        legacy = rooms.legacy_scenes([{'id': 'user-3', 'name': 'Plain', 'source_id': 'truckersfm'}], self.sources, self.presets)
        recipe = legacy[0]['args']['recipe']
        self.assertEqual(recipe['picture'], {'preset_id': None, 'enabled': False, 'placement': '9', 'still': False})
        self.assertFalse(legacy[0]['scene_available'])

    def test_the_classic_hub_reads_the_same_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(room_catalogue.hub, 'STATE', Path(folder)):
                room_catalogue.hub.save_json(Path(folder) / 'scenes.json', [self.scenes[1]])
                found = room_catalogue.hub.scenes()
        self.assertIn('user-1', [scene['id'] for scene in found])
        self.assertEqual(names(rooms.legacy_scenes(found, self.sources, self.presets, self.catalogue)), ['Road trip'])


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'), 'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)
        self.backend = FakeBackend()
        self.controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: 1000.)
        self.controller._home_action = mock.Mock()
        self.controller._route_preferred = mock.Mock()
        self.addCleanup(self.controller.close)
        self.service = session_service.Service(self.controller)
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=[{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/p'}]),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=[
                          {'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'}])):
            patch.start()
            self.addCleanup(patch.stop)

    def settle(self):
        self.controller.worker.submit(lambda: None).result(timeout=3)
        self.controller.refresh()

    def save(self, name):
        self.controller.enter({'room_id': 'library'})
        self.settle()
        return self.controller.dispatch('save', {'name': name})['variant']

    def test_rename_keeps_the_room_and_changes_its_name_and_id(self):
        saved = self.save('Rainy reading')
        result = self.service.execute('rename', {'id': saved['id'], 'name': 'Quiet reading'})
        self.assertEqual(result['variant']['name'], 'Quiet reading')
        variants = self.controller.snapshot()['variants']
        self.assertEqual([item['name'] for item in variants], ['Quiet reading'])
        self.assertEqual(variants[0]['id'], rooms.variant_id('Quiet reading'))
        self.assertEqual(variants[0]['music'], saved['music'])
        self.assertEqual(self.controller.snapshot()['active']['name'], 'Quiet reading')
        self.assertEqual(self.controller.snapshot()['last_session']['id'], variants[0]['id'])

    def test_rename_does_not_start_or_change_playback(self):
        saved = self.save('Rainy reading')
        calls = len(self.backend.calls)
        self.service.execute('rename', {'id': saved['id'], 'name': 'Quiet reading'})
        self.assertEqual(len(self.backend.calls), calls)

    def test_rename_refuses_blank_unknown_and_taken_names(self):
        first = self.save('Rainy reading')
        second = self.save('Morning radio')
        for args in ({'id': first['id'], 'name': '  '}, {'id': 'nope', 'name': 'X'}, {'id': first['id'], 'name': 'Morning radio'}):
            with self.assertRaises(sessions.SessionError):
                self.service.execute('rename', args)
        self.assertEqual(sorted(item['name'] for item in self.controller.snapshot()['variants']), ['Morning radio', 'Rainy reading'])
        self.assertEqual(second['name'], 'Morning radio')

    def test_delete_removes_only_that_saved_room(self):
        first = self.save('Rainy reading')
        self.save('Morning radio')
        self.service.execute('delete', {'id': first['id']})
        self.assertEqual([item['name'] for item in self.controller.snapshot()['variants']], ['Morning radio'])


if __name__ == '__main__':
    unittest.main()
