"""Room-level actions shared by every surface that controls the room: Now, the mini
player and the Waybar popover. Each surface supplies how to send a command and how
to confirm; the wording, the commands and the Undo rules live here once."""
import next_model as model


class Ticket:
    """One command in flight, handed back to whoever sent it.

    ``add_finish(callback)`` runs the callback once the command ends, either way (on the GTK
    thread); a pressed control uses it to re-enable itself. This replaces an attribute that the
    next command sent, whichever it was, used to pick up."""
    def __init__(self):
        self.done = False
        self.callbacks = []

    def add_finish(self, callback):
        if self.done:
            callback()
        else:
            self.callbacks.append(callback)
        return self

    def finish(self):
        if self.done:
            return
        self.done = True
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def when_finished(ticket, callback):
    """Run ``callback`` when ``ticket`` (what a send returned) ends, or now when nothing was sent."""
    if isinstance(ticket, Ticket):
        ticket.add_finish(callback)
    else:
        callback()


class RoomActions:
    def __init__(self, send, toast, snapshot):
        """send(action, args=None, done=None, urgent=False, quiet=False);
        toast(title, undo_label=None, undo=None); snapshot() returns the latest snapshot."""
        self.send, self.toast, self.snapshot = send, toast, snapshot

    def toggle(self):
        """Pause is Quiet, Play is Resume; with a room but no sound, enter it again (controller `transport`).

        Returns what ``send`` returned (a Ticket from the windows), or None when nothing was sent."""
        snapshot = self.snapshot()
        if not snapshot.get('active'):
            return None
        state = model.room_state(snapshot)
        if state == 'playing':
            return self.send('quiet', urgent=True)
        if state == 'paused':     # resume plays the offer, or every paused layer when there is none
            return self.send('resume')
        return self.send('enter', {'recipe': snapshot['active']})

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
