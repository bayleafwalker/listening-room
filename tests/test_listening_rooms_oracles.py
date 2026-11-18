"""Independent acceptance oracles for O2 that need no GTK: rename references, id agreement, persistence.

Owned by the acceptance author. They exercise the real Controller and the model's id prediction side by side,
because the Rooms dialogs decide "this replaces / you already have" from the model's prediction while the
controller decides from its own hash.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_rooms_model as rooms
import room_session as sessions
import session_service
from test_listening_session import FakeBackend


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'), 'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)
        for patch in (mock.patch.object(sessions.hub, 'sources', return_value=[{'id': 'piano', 'name': 'Piano', 'kind': 'radio', 'url': 'https://example.test/p'}]),
                      mock.patch.object(sessions.room_catalogue, 'rooms', return_value=[
                          {'id': 'library', 'name': 'Midnight library', 'source_id': 'piano', 'view_id': 'room-library'}])):
            patch.start()
            self.addCleanup(patch.stop)
        self.controller = self.make()
        self.service = session_service.Service(self.controller)

    def make(self):
        controller = sessions.Controller(FakeBackend(), self.tmp.name, clock=lambda: 1000.)
        controller._home_action = mock.Mock()
        controller._route_preferred = mock.Mock()
        self.addCleanup(controller.close)
        return controller

    def settle(self, controller=None):
        controller = controller or self.controller
        controller.worker.submit(lambda: None).result(timeout=3)
        controller.refresh()

    def save(self, name):
        self.controller.enter({'room_id': 'library'})
        self.settle()
        return self.controller.dispatch('save', {'name': name})['variant']


class RenameReferences(Base):
    def test_renamed_active_room_leaves_no_dangling_reference_and_survives_restart(self):
        old = self.save('Evening reading')
        self.save('Other')
        self.controller.enter({'variant_id': old['id']})
        self.settle()
        renamed = self.service.execute('rename', {'id': old['id'], 'name': 'Late reading'})['variant']
        snapshot = self.controller.snapshot()
        live = {item['id'] for item in snapshot['variants']}
        self.assertEqual(snapshot['active']['id'], renamed['id'])
        self.assertEqual(snapshot['last_session']['id'], renamed['id'])
        self.assertNotIn(old['id'], live | set(snapshot['recent']) | {snapshot['active']['id'], snapshot['last_session']['id']})
        self.controller.close()
        again = self.make()
        self.assertEqual({item['id'] for item in again.snapshot()['variants']}, live)
        reloaded = again.snapshot()
        self.assertNotIn(old['id'], reloaded['recent'])
        self.assertEqual((reloaded['last_session'] or {}).get('id'), renamed['id'])
        again.enter({'variant_id': renamed['id']})
        self.settle(again)
        self.assertEqual(again.snapshot()['active']['name'], 'Late reading')
        with self.assertRaises(sessions.SessionError):
            again.enter({'variant_id': old['id']})

    def test_rename_keeps_the_card_position(self):
        first = self.save('Aaa')
        self.save('Bbb')
        self.save('Ccc')
        self.service.execute('rename', {'id': first['id'], 'name': 'Zzz'})
        self.assertEqual([v['name'] for v in self.controller.snapshot()['variants']], ['Zzz', 'Bbb', 'Ccc'])

    def test_renaming_to_the_same_name_is_harmless(self):
        saved = self.save('Same')
        result = self.service.execute('rename', {'id': saved['id'], 'name': 'Same'})
        self.assertEqual(result['variant']['id'], saved['id'])
        self.assertEqual(len(self.controller.snapshot()['variants']), 1)

    def test_rename_cannot_take_a_name_that_only_differs_in_whitespace(self):
        first = self.save('Evening reading')
        second = self.save('Other')
        with self.assertRaises(sessions.SessionError):
            self.service.execute('rename', {'id': second['id'], 'name': ' Evening \t reading '})
        self.assertEqual(sorted(v['name'] for v in self.controller.snapshot()['variants']), ['Evening reading', 'Other'])
        self.assertEqual(first['name'], 'Evening reading')


class IdAgreement(Base):
    """What the dialogs predict (model.variant_id on the typed, stripped name) must be the controller's id."""

    NAMES = ['Plain', 'Évening', 'two  spaces', 'tab\tname', 'x' * 300, 'trailing ']

    def test_model_prediction_matches_the_controller_for_save(self):
        wrong = []
        for name in self.NAMES:
            typed = name.strip()           # what the dialogs pass on
            saved = self.save(name)
            if rooms.variant_id(typed) != saved['id']:
                wrong.append((name, rooms.variant_id(typed), saved['id']))
        self.assertEqual(wrong, [], 'the dialogs cannot tell when a name collides, so "Undo" can delete an existing room')

    def test_model_prediction_matches_the_controller_for_rename(self):
        base = self.save('Base')
        wrong = []
        for name in self.NAMES:
            renamed = self.service.execute('rename', {'id': base['id'], 'name': name})['variant']
            base = renamed
            if rooms.variant_id(name.strip()) != renamed['id']:
                wrong.append((name, rooms.variant_id(name.strip()), renamed['id']))
        self.assertEqual(wrong, [])


if __name__ == '__main__':
    unittest.main()
