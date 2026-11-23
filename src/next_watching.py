"""Watch: a YouTube video with sound in its own player window, beside the room.

The controller's ``video`` action opens the window (ambience mode ``video``) and pauses the
room's Music and Ambience the way Quiet does. This module is the window side: the
"Watching · title" chip with Stop watching on Now, and the Resume offer once the video
stops. Resume is never automatic; it is a toast button the listener presses.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from next_widgets import label


class Watching:
    """Owns the Watching chip and turns snapshot changes of ``video`` into words."""

    def __init__(self, window):
        self.window = window
        self.shown = None          # the video the chip shows (a snapshot ``video``), or None
        self.error = None          # the last ``errors.video`` already said
        self.alone = False         # Now shows the video with no room behind it
        self.chip = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.chip.add_css_class('watching-chip')
        self.text = label('', ['source-chip'], ellipsize=True)
        self.text.set_max_width_chars(48)
        self.chip.append(self.text)
        self.stop_button = Gtk.Button(label='Stop watching', valign=Gtk.Align.CENTER)
        self.stop_button.add_css_class('pill')
        self.stop_button.connect('clicked', lambda *_: self.stop())
        self.chip.append(self.stop_button)
        self.chip.set_visible(False)

    # ---- actions -------------------------------------------------------------------------

    def watch(self, item):
        """Open ``item`` (a YouTube row or link) in the Watch window. Returns the command's Ticket."""
        url = (item or {}).get('url') or ''
        name = (item or {}).get('name') or 'YouTube video'
        room = (self.window.snapshot or {}).get('active')
        ticket = self.window.command('video', {'action': 'start', 'url': url, 'name': name})
        if room:
            # The room pauses; Now says so, with Stop watching.
            self.window.show_page('now')
            self.window.toast('Watching %s. %s is paused.' % (name, room.get('name') or 'The room'))
        else:
            # Nothing else changes; Now carries the chip and Stop watching when the listener goes there.
            self.window.toast('Watching ' + name, 'Show in Now', self.window.show_now)
        return ticket

    def stop(self):
        return self.window.command('video', {'action': 'stop'}, urgent=True)

    # ---- snapshot ------------------------------------------------------------------------

    def update(self, snapshot):
        snapshot = snapshot or {}
        video = snapshot.get('video') if isinstance(snapshot.get('video'), dict) else None
        before, self.shown = self.shown, video
        self.chip.set_visible(bool(video))
        if video:
            self.text.set_text('Watching · ' + (video.get('name') or 'YouTube video')
                               + (' · Paused' if video.get('status') == 'Paused' else ''))
            self.text.set_tooltip_text(video.get('name') or '')
        self.render_without_room(snapshot, video)
        error = (snapshot.get('errors') or {}).get('video')
        if error and error != self.error:
            self.window.error_toast(error)
        self.error = error
        if before and not video:
            self.stopped(snapshot)

    def render_without_room(self, snapshot, video):
        """Now while a video plays with no room: the chip and Stop watching, and no room controls."""
        window = self.window
        alone = bool(video) and not snapshot.get('active') and not window.pending
        window.controls.set_visible(not alone)
        if not alone:
            if self.alone and window.current_page() == 'now' and not snapshot.get('active'):
                window.show_rooms()           # the video stopped and there is no room to show
            self.alone = False
            return
        self.alone = True
        window.chip.set_visible(False)
        window.changed_chip.set_visible(False)
        window.choose_scene.set_visible(False)
        window.room_name.set_text(video.get('name') or 'YouTube video')
        window.now_line.set_text('Playing with sound in its own window. No room is playing.')
        window.now_detail.set_visible(False)
        # Nothing of the last room stays behind the video's chip: no scene art, no room accent.
        window.scene.show_room({'still': None, 'poster': None, 'loop': None}, False)
        window.apply_accent(None)

    def stopped(self, snapshot):
        """The video stopped (window closed, video ended or Stop watching): offer Resume, never resume."""
        room = snapshot.get('active')
        offer = snapshot.get('resume_offer')
        if not room or not offer or offer.get('session_id') != room.get('id'):
            return
        name = room.get('name') or 'the room'
        self.window.toast('Resume %s?' % name, 'Resume', lambda: self.window.command('resume'))
