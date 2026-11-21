"""Track details for one library track: what is known, matching, and the exact-recording review.

The evidence rules are the metadata contract's. Identity candidates
are suggestions, confirmation is separate from reviewing audio, and a review is
bound to the audio version the controller hashed. None of these actions starts
music: the dialog only sends ``metadata.*`` and reads the session snapshot.
"""
import json

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import next_prefs
from next_async import pool, run_async
import next_widgets
import session_client

METHODS = ('MusicBrainz text search', 'AcoustID audio fingerprint')
CLASSES = (('instrumental', 'Instrumental (no vocals or speech)'),
           ('vocal', 'Vocals'), ('spoken', 'Spoken word'), ('unknown', 'Unknown'))
ENRICH_TIP = 'Collects reported recording IDs plus any existing tempo, energy and mood estimates.'
MATCH_TIP = 'Sends track details to MusicBrainz, or an audio fingerprint to AcoustID.'
ANALYZE_TIP = 'Downloads the complete original and runs the packaged CPU voice/instrumental models.'
MATCH_HELP = ('Match sends track details to MusicBrainz, or an audio fingerprint to AcoustID. AcoustID needs a '
              'configured application key and the fingerprint tool.')
MATCH_NOTE = 'Identity candidates are suggestions. Confirming an identity is separate from reviewing the audio.'
REVIEW_EXPLANATION = ('Listen to the whole exact recording yourself before reviewing. The review applies only to the '
                      'audio version checked by the controller; replacement audio needs a new review. Reviewing does '
                      'not start playback.')
REVIEW_ACK = 'I listened to the entire exact recording'
EVIDENCE_NOTE = ('Reported identities and estimates describe what a service said about this audio. They are data, '
                 'not a review.')


def record_lines(reference, record, snapshot=None):
    """Everything the overview says about one track, in the listener's words."""
    reference = reference or {}
    lines = [str(reference.get('name') or 'Track'), str(reference.get('artist') or 'Artist unknown')]
    if reference.get('album'):
        lines.append(str(reference['album']))
    station = (snapshot or {}).get('station') or {}
    reasons = station.get('explanation') or []
    if (station.get('current') or {}).get('id') == reference.get('id') and reasons:
        lines.append('Why this selection: ' + '; '.join(str(reason) for reason in reasons))
    elif snapshot is not None:
        lines.append('Why this selection appears while it is the station’s current track.')
    if not record:
        lines.append('No metadata for this recording yet. Choose Enrich, Match or Analyze.')
        return lines
    lines.append(('Audio version: SHA256 ' + str(record['audio_sha256'])) if record.get('audio_sha256')
                 else 'Audio version has not been fingerprinted yet.')
    identities = record.get('identities') or []
    for obs in identities:
        lines.append('Identity: ' + str(obs.get('name') or obs.get('provider_id') or obs.get('value') or 'Unknown')
                     + ' (source: ' + str(obs.get('provider', 'unknown'))
                     + ', confidence: ' + str(obs.get('confidence', 'unknown'))
                     + ', status: ' + str(obs.get('status', 'unconfirmed')) + ')')
    if not identities:
        lines.append('Recording identity not matched.')
    for obs in record.get('features') or []:
        name = str(obs.get('name', 'feature'))
        source = str(obs.get('provider', 'unknown'))
        if name in ('instrumental score', 'voice score'):
            scope = ('whole recording, audio version above'
                     if record.get('audio_sha256') and obs.get('provider') == 'Essentia MTG'
                     else 'audio version unknown')
            lines.append('Uncalibrated model score: ' + name + ' = ' + str(obs.get('value', 'unknown'))
                         + ' (source: ' + source + '; ' + scope + '; not a probability or human review)')
        else:
            lines.append('Estimate: ' + name + ' = ' + str(obs.get('value', 'unknown'))
                         + ' (source: ' + source + ', confidence: ' + str(obs.get('confidence', 'unknown')) + ')')
    for obs in record.get('unbound_features') or []:
        lines.append('Uncalibrated estimate: ' + str(obs.get('name', 'feature')) + ' = '
                     + str(obs.get('value', 'unknown')) + ' (source: ' + str(obs.get('provider', 'unknown'))
                     + '; audio version unknown)')
    if not record.get('features') and not record.get('unbound_features'):
        lines.append('No audio estimates available.')
    review = record.get('review')
    lines.append(('Human review: ' + str(review.get('classification')) + ' by ' + str(review.get('reviewer'))
                  + ' on ' + str(review.get('date'))) if review else 'No human review for this audio version.')
    lines.append('Last checked: ' + str(record.get('updated_at', 'unknown'))
                 + '. Review validity is checked again before use.')
    return lines


def candidate_labels(candidates):
    """Dropdown text for identity candidates; the provider keeps them distinguishable."""
    labels = [str(item.get('title') or item.get('provider_id')) + ' (' + str(item.get('provider', 'unknown')) + ')'
              for item in candidates or [] if isinstance(item, dict)]
    return labels or ['No identity candidates']


def match_candidates(jobs, job_id):
    """Candidates from the finished match job this dialog started."""
    job = next((item for item in jobs or [] if isinstance(item, dict) and item.get('job_id') == job_id), None)
    if not job or job.get('status') != 'completed':
        return []
    return ((job.get('result') or {}).get('candidates') or [])


def review_problem(reference, record, reviewer, acknowledged, classification=None):
    """The first thing standing between the form and a saved review, or None."""
    if not reference:
        return 'Open a library track first.'
    if not record:
        return 'Load this recording’s details first.'
    if classification is None:
        return 'Choose what this recording is.'
    if not acknowledged:
        return 'Listen to the whole recording and confirm the review scope.'
    if not str(reviewer or '').strip():
        return 'Enter your name or review identifier.'
    return None


def jobs_words(jobs):
    active = [job for job in jobs or [] if isinstance(job, dict) and job.get('status') in ('queued', 'running')]
    if not active:
        return 'No metadata jobs.'
    job = active[0]
    return (str(job.get('kind') or 'Metadata').capitalize() + ': ' + str(job.get('status'))
            + ' (' + str(len(active)) + ' pending)')


class TrackDetailsDialog(Adw.Dialog):
    """Metadata, matching and the exact-recording review for one Jellyfin track."""

    def __init__(self, window, reference):
        super().__init__()
        self.window = window
        self.reference = dict(reference) if isinstance(reference, dict) else None
        self.jellyfin = bool(self.reference and self.reference.get('provider') == 'jellyfin'
                             and self.reference.get('type') == 'Audio')
        self.record = None
        self.jobs = []
        self.match_jobs = {}
        self.candidates = []
        self.candidate_fingerprint = None
        self.closed = self.pending = False
        self.acoustid_ready = False
        self.executor = pool('room-track-details', 2)
        self.set_title('Track details')
        self.set_content_width(520)
        self.set_content_height(620)
        next_widgets.install_css(window.get_display(), next_prefs.CSS, 2)
        self.build()
        try:
            self.connect('closed', lambda *_: self.cleanup())
        except TypeError:
            pass
        self.refresh()
        self.timer = GLib.timeout_add_seconds(2, self.refresh)

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---- structure ----------------------------------------------------------

    def title_words(self):
        reference = self.reference or {}
        name = reference.get('name') or 'Track details'
        return name, str(reference.get('artist') or '')

    def build(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        name, artist = self.title_words()
        header.set_title_widget(Adw.WindowTitle(title=name, subtitle=artist))
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        self.detail = next_widgets.label('Loading this recording…', ['prefs-note'], wrap=True)
        self.detail.set_selectable(True)
        self.detail.set_margin_top(10)
        self.detail.set_margin_bottom(10)
        self.detail.set_margin_start(12)
        self.detail.set_margin_end(12)
        overview = Adw.PreferencesGroup(title='Overview')
        text_row = Adw.PreferencesRow(activatable=False)
        text_row.set_child(self.detail)
        overview.add(text_row)
        overview.add(next_prefs.paragraph(EVIDENCE_NOTE))
        page.add(overview)

        actions = Adw.PreferencesGroup(title='Recording actions', description=(
            'Each action is explicit and none of them starts music.'))
        buttons = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.action_buttons = {}
        for title, tip, mode in (('Enrich', ENRICH_TIP, 'enrich'), ('Match', MATCH_TIP, 'match'),
                                 ('Analyze', ANALYZE_TIP, 'analyze')):
            button = Gtk.Button(label=title, sensitive=self.jellyfin)
            button.set_tooltip_text(tip)
            button.connect('clicked', lambda _b, chosen=mode: self.start(chosen))
            buttons.append(button)
            self.action_buttons[title] = button
        actions.add(self.wrap(buttons))
        page.add(actions)

        match = Adw.PreferencesGroup(title='Match')
        self.method_row = Adw.ComboRow(title='Match using', model=Gtk.StringList.new(list(METHODS)))
        self.method_row.set_sensitive(self.jellyfin)
        match.add(self.method_row)
        match.add(next_prefs.paragraph(MATCH_HELP))
        self.candidates_label = next_widgets.label('No identity candidates yet. Run Match first.', ['prefs-note'])
        self.candidates_label.set_margin_top(8)
        self.candidates_label.set_margin_bottom(8)
        self.candidates_label.set_margin_start(12)
        self.candidates_label.set_margin_end(12)
        match.add(self.wrap(self.candidates_label))
        self.candidate_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                                     margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.candidate_box.set_visible(False)
        self.candidate_buttons = []
        match.add(self.wrap(self.candidate_box))
        match.add(next_prefs.button_row('Confirm identity', 'Confirm', self.confirm,
                                        subtitle='Only a candidate from a finished match can be confirmed.'))
        match.add(next_prefs.paragraph(MATCH_NOTE))
        page.add(match)

        review = Adw.PreferencesGroup(title='Review this exact recording')
        review.add(next_prefs.paragraph(REVIEW_EXPLANATION))
        review.add(next_prefs.paragraph('Instrumental means no speaking, singing or vocal samples.'))
        self.class_buttons = []
        class_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        first = None
        for _key, label_text in CLASSES:
            button = Gtk.CheckButton(label=label_text)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect('toggled', lambda *_: self.review_status.set_text(''))
            class_box.append(button)
            self.class_buttons.append(button)
        review.add(self.wrap(class_box))
        self.reviewer = Adw.EntryRow(title='Reviewer name or identifier')
        self.reviewer.connect('changed', lambda *_: self.review_status.set_text(''))
        review.add(self.reviewer)
        self.ack = Gtk.CheckButton(label=REVIEW_ACK, margin_top=8, margin_bottom=8, margin_start=12)
        self.ack.set_active(False)
        self.ack.connect('toggled', lambda *_: self.review_status.set_text(''))
        review.add(self.wrap(self.ack))
        buttons = Gtk.Box(spacing=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.save_review = Gtk.Button(label='Save review', sensitive=False)
        self.save_review.connect('clicked', lambda *_: self.review())
        self.revoke_review = Gtk.Button(label='Revoke review', sensitive=False)
        self.revoke_review.connect('clicked', lambda *_: self.revoke())
        buttons.append(self.save_review)
        buttons.append(self.revoke_review)
        review.add(self.wrap(buttons))
        self.review_status = next_widgets.label('', ['prefs-note'], wrap=True)
        self.review_status.set_margin_start(12)
        self.review_status.set_margin_end(12)
        self.review_status.set_margin_bottom(8)
        review.add(self.wrap(self.review_status))
        page.add(review)

        self.progress = next_widgets.label('No metadata jobs.', ['prefs-note'], wrap=True)
        self.progress.set_margin_start(12)
        self.progress.set_margin_end(12)
        self.progress.set_margin_top(8)
        self.progress.set_margin_bottom(8)
        jobs = Adw.PreferencesGroup(title='Jobs')
        jobs.add(self.wrap(self.progress))
        page.add(jobs)
        toolbar.set_content(page)
        self.set_child(toolbar)

    @staticmethod
    def wrap(widget):
        row = Adw.PreferencesRow(activatable=False)
        row.set_child(widget)
        row.set_activatable(False)
        return row

    def close_dialog(self):
        self.cleanup()
        Adw.Dialog.close(self)

    # ---- state --------------------------------------------------------------

    def refresh(self):
        if self.closed or self.pending:
            return True
        if not self.reference:
            self.detail.set_text('Open a library track to see its details.')
            return True
        self.pending = True
        reference = dict(self.reference)

        def work():
            record = session_client.request('metadata.lookup_reference', {'reference': reference}, timeout=3).get('record')
            jobs = session_client.request('metadata.jobs', {}, timeout=3).get('jobs') or []
            status = session_client.request('metadata.status', {}, timeout=3)
            return record, jobs, status

        run_async(work, lambda value: self.apply(value, None),
                  lambda exc: self.apply(None, str(exc) or 'Metadata is unavailable'), self.executor)
        return True

    def apply(self, value, error):
        self.pending = False
        if self.closed:
            return False
        if error:
            self.progress.set_text('Metadata unavailable: ' + error)
            return False
        record, jobs, status = value
        self.record = record
        self.jobs = [job for job in jobs if isinstance(job, dict)]
        self.acoustid_ready = bool(status.get('acoustid_ready'))
        self.render()
        return False

    def render(self):
        snapshot = self.window.snapshot
        self.detail.set_text('\n'.join(record_lines(self.reference, self.record, snapshot)))
        job_id = self.match_jobs.get((self.reference or {}).get('id'))
        candidates = match_candidates(self.jobs, job_id)
        fingerprint = json.dumps(candidates, sort_keys=True)
        if fingerprint != self.candidate_fingerprint:
            self.candidate_fingerprint = fingerprint
            self.candidates = candidates
            self.render_candidates()
        for button in self.action_buttons.values():
            button.set_sensitive(self.jellyfin)
        self.save_review.set_sensitive(bool(self.jellyfin and self.record))
        self.revoke_review.set_sensitive(bool(self.jellyfin and self.record and (self.record or {}).get('review')))
        self.progress.set_text(jobs_words(self.jobs))
        if not self.jellyfin:
            self.review_status.set_text('Details and reviews are for Jellyfin audio tracks.')

    def render_candidates(self):
        """Identity candidates as full-length choices, each naming its provider."""
        for button in self.candidate_buttons:
            self.candidate_box.remove(button)
        self.candidate_buttons = []
        labels = candidate_labels(self.candidates) if self.candidates else []
        self.candidates_label.set_visible(not labels)
        self.candidate_box.set_visible(bool(labels))
        first = None
        for label in labels:
            button = Gtk.CheckButton(label=label)
            if first is None:
                first = button
            else:
                button.set_group(first)
            self.candidate_box.append(button)
            self.candidate_buttons.append(button)

    def selected_candidate(self):
        for index, button in enumerate(self.candidate_buttons):
            if button.get_active() and index < len(self.candidates):
                return self.candidates[index]
        return None

    # ---- actions ------------------------------------------------------------

    def start(self, mode):
        if not self.jellyfin:
            return
        reference = dict(self.reference)
        params = {'reference': reference}
        if mode == 'match':
            params['method'] = 'acoustid' if self.method_row.get_selected() else 'musicbrainz'
            if not self.acoustid_ready and params['method'] == 'acoustid':
                self.review_status.set_text('Add an AcoustID application key in Preferences › Recordings first.')
                return

        def accepted(result):
            if mode == 'match' and isinstance(result, dict):
                self.match_jobs[reference['id']] = result.get('job_id')
            self.refresh()
        self.window.command('metadata.' + mode, params, done=accepted)

    def confirm(self):
        reference = self.reference or {}
        job_id = self.match_jobs.get(reference.get('id'))
        candidate = self.selected_candidate()
        if not job_id or candidate is None:
            self.review_status.set_text('Run Match and choose a candidate from its results first.')
            return
        self.window.command('metadata.confirm_identity',
                            {'job_id': job_id, 'provider_id': candidate.get('provider_id')},
                            done=lambda _result: self.refresh())

    def classified(self):
        for index, button in enumerate(self.class_buttons):
            if button.get_active():
                return CLASSES[index][0]
        return None

    def review(self):
        classification = self.classified()
        problem = review_problem(self.reference, self.record, self.reviewer.get_text(),
                                 self.ack.get_active(), classification)
        if problem:
            self.review_status.set_text(problem)
            return
        if self.pending:
            return

        def saved(_result):
            self.ack.set_active(False)
            self.reviewer.set_text('')
            for button in self.class_buttons:
                button.set_active(False)
            self.review_status.set_text('Review saved for this audio version.')
            self.refresh()
        self.window.command('metadata.review', {'origin': self.record['origin'], 'item_id': self.record['item_id'],
                                                'classification': classification,
                                                'reviewer': self.reviewer.get_text().strip(),
                                                'acknowledged': True}, done=saved)

    def revoke(self):
        if not self.record:
            return

        def revoked(_result):
            self.review_status.set_text('Review revoked.')
            self.refresh()
        self.window.command('metadata.revoke', {'origin': self.record['origin'], 'item_id': self.record['item_id']},
                            done=revoked)
