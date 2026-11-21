"""Behavior boundaries for desktop controls and picture recovery."""
import os
from pathlib import Path
import random
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ambience
import listening_hub as hub
import player_core as core
import roomctl


class DesktopTests(unittest.TestCase):
    def test_waybar_reports_the_active_room_and_both_sounding_layers(self):
        snapshot = {'active': {'name':'Library & rain'}, 'layers': {'music': {'name':'Piano','status':'Playing'}, 'atmosphere': {'name':'Rain','status':'Playing'}}}
        result = roomctl.status(snapshot)
        self.assertIn('Library &amp; rain', result['text'])
        self.assertIn('Piano + Rain', result['text'])
        self.assertEqual(result['class'], 'playing')
        self.assertEqual(roomctl.status({})['class'], 'stopped')

    def test_radio_dispatches_through_the_shared_session(self):
        source={'id':'station','name':'Station','kind':'radio','url':'https://example.org/live'}
        with patch('session_client.request') as dispatch, patch.object(core,'start_radio') as start:
            hub.play(source)
        dispatch.assert_called_once_with('music', {'item':source})
        start.assert_not_called()

    def test_media_keys_act_on_the_room_like_the_windows_play_pause(self):
        # O10: the room's radio player is its music; keys pause and resume the room (every layer).
        expected = {'play-pause': ('transport', {'action': 'toggle'}), 'pause': ('transport', {'action': 'pause'}),
                    'play': ('transport', {'action': 'play'}), 'stop': ('leave', {}),
                    'next': ('layer', {'layer': 'music', 'action': 'next'})}
        for command, call in expected.items():
            with patch('session_client.request') as dispatch:
                core.transport('radio', command)
            dispatch.assert_called_once_with(*call)

    def test_media_key_with_only_the_rooms_ambience_sounding_reaches_the_room(self):
        sent = []
        def request(action, args=None, **_):
            sent.append(action)
            return {'active': {'name': 'Room'}} if action == 'inspect' else {}
        with patch('session_client.request', side_effect=request):
            self.assertTrue(hub.room_transport('play-pause'))
        self.assertEqual(sent, ['inspect', 'transport'])
        with patch('session_client.request', return_value={'active': None}) as dispatch:
            self.assertFalse(hub.room_transport('play-pause'))
        dispatch.assert_called_once()

    def test_roomctl_transport_and_layer_verbs(self):
        self.assertEqual(roomctl.transport_arguments('toggle'), ('transport', {'action': 'toggle'}))
        self.assertEqual(roomctl.transport_arguments('pause'), ('transport', {'action': 'pause'}))
        self.assertEqual(roomctl.transport_arguments('play'), ('transport', {'action': 'play'}))
        self.assertEqual(roomctl.transport_arguments('music', 'pause'), ('layer', {'layer': 'music', 'action': 'pause'}))
        self.assertEqual(roomctl.transport_arguments('ambience', 'stop'), ('layer', {'layer': 'atmosphere', 'action': 'stop'}))
        for action, value in (('music', None), ('ambience', 'select'), ('toggle', 'x')):
            with self.assertRaises(ValueError):
                roomctl.transport_arguments(action, value)

    def test_media_discovery_excludes_own_mpris_identity(self):
        with patch.object(core, 'run') as run:
            run.return_value.returncode=0
            run.return_value.stdout='ListeningRoom\nListeningRoom.instance1\nfirefox.instance2\n'
            self.assertEqual(hub.players(), ['firefox.instance2'])

    def test_missing_display_falls_back_to_current_workspace(self):
        with patch.object(ambience,'monitors',return_value=[]), patch.object(ambience,'place') as place, patch.object(ambience,'_run') as command:
            result=ambience.place_on_display('unplugged-display')
        place.assert_called_once_with('here','ambience'); command.assert_not_called()
        self.assertTrue(result['fallback'])

    def test_cancelled_picture_never_stops_or_starts_players(self):
        event=threading.Event();event.set()
        with patch.object(ambience,'_run') as command:
            with self.assertRaisesRegex(ambience.AmbienceError,'cancelled'):
                ambience.start('unimportant',cancel_event=event)
        command.assert_not_called()

    def test_picture_brightness_rejects_invalid_values_without_touching_player(self):
        for value in (-1, 2, float('nan'), float('inf'), 'dark'):
            with self.subTest(value=value), patch.object(ambience,'_ipc') as ipc:
                with self.assertRaises(ambience.AmbienceError): ambience.set_lighting(value)
                ipc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
