"""Room-level actions shared by every surface that controls the room: Now, the mini
player and the Waybar popover. Each surface supplies how to send a command and how
to confirm; the wording, the commands and the Undo rules live here once."""
import next_model as model


class RoomActions:
    def __init__(self, send, toast, snapshot):
        """send(action, args=None, done=None, urgent=False, quiet=False);
        toast(title, undo_label=None, undo=None); snapshot() returns the latest snapshot."""
        self.send, self.toast, self.snapshot = send, toast, snapshot

    def toggle(self):
        """Pause is Quiet, Play is the existing resume; with a room but no sound, enter it again."""
        snapshot = self.snapshot()
        if not snapshot.get('active'):
            return
        if model.room_state(snapshot) == 'playing':
            self.send('quiet', urgent=True)
        elif snapshot.get('resume_offer'):
            self.send('resume')
        else:
            self.send('enter', {'recipe': snapshot['active']})

    def leave(self):
        name = (self.snapshot().get('active') or {}).get('name', 'the room')
        self.send('leave', {}, urgent=True,
                  done=lambda _r: self.toast('Left ' + name, 'Return', lambda: self.send('return')))

    def timer(self, kind):
        send, toast = self.send, self.toast
        if kind == 'break':
            send('quiet', {'minutes': 10}, urgent=True,
                 done=lambda _r: toast('Break for 10 minutes. Play is up to you.', 'Undo', lambda: send('resume')))
        elif kind == 'wind_down':
            send('wind_down', {'minutes': 30},
                 done=lambda _r: toast('Winding down over 30 minutes', 'Undo',
                                       lambda: send('wind_down', {'minutes': 0})))
        elif kind == 'dim':
            send('layer', {'layer': 'picture', 'action': 'sequence', 'minutes': 30},
                 done=lambda _r: toast('Dimming the scene over 30 minutes', 'Undo',
                                       lambda: send('layer', {'layer': 'picture', 'action': 'sequence', 'minutes': 0})))
        elif kind == 'cancel':
            running = {item['kind'] for item in model.timers(self.snapshot())}
            if 'wind_down' in running:
                send('wind_down', {'minutes': 0})
            if 'dim' in running:
                send('layer', {'layer': 'picture', 'action': 'sequence', 'minutes': 0})
            if 'break' in running:
                send('resume')
            toast('Timer cancelled')
