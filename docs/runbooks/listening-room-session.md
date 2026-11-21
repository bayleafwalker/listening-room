# Listening Room sessions and integrations

Listening Room uses one local session controller for the GTK app, command-line
controls and Waybar. The main navigation is **Listen**, **Room** and **Recent**.
Listen is the direct-first source picker; Room shows the active room or the
room chooser before explicit entry; Recent shows entered stations and rooms. Selecting or editing a source
does not start playback. Choose Tune in or Enter room when ready. These
instructions describe source behavior, not proof of a connected provider account.

## Listen: stations, sources and history

Use Listen to browse radio, Jellyfin library items, links and live channels.
Previewing or editing is inert. Choose Tune in to start. Save a selected source
as a station to reuse it, then use Your stations to edit its name, approved
fallback sources and library preferences. Stations save references and user
intent, not prepared stream URLs or credentials. A private custom stream must
first be saved as a favorite. A Jellyfin station stores library references.

Library stations prepare a rolling queue of up to eight tracks from the
selected collection. Upcoming tracks are shown in Listen; Play next inserts a
selected Jellyfin track. When one track ends, the controller advances the
queue. Stop ends the station when its candidates are exhausted; Repeat refills
from that same collection. If a source cannot start, the controller tries the
station's approved fallbacks in order. A failed music candidate keeps the
previous Main audio source and its controls. Reconnect uses the station's
selected sources rather than silently choosing a new provider. AudioMuse
fetches candidates for the initial bounded pool; later queue refills do not
request another mix. For a Jellyfin collection sent to Background audio,
end-of-track advances that layer's own queue independently of Main audio.

Station preferences for energy, familiarity, speech and range guide later
tracks using listening history and available metadata estimates. Likely
instrumental can analyze a bounded set of candidates in the background and
prefer higher instrumental model scores; scores are uncalibrated and can be
wrong. Strict instrumental requires a local whole-recording human review and
plays the exact verified audio snapshot. Unknown, changed or revoked recordings
are excluded, including queue overrides, replay and fallbacks. More like this and Less like this
feedback persists across restarts and updates future choices. Reset station
memory clears preference memory without deleting replay history. Wind down or
Rise changes the energy target every three tracks. Hold mood pauses that arc.
These heuristics are optional and do not affect other music sources.

Recent records station sessions and rooms entered. Replay is enabled only when
the selected history has track references; live radio that reports no track
references remains visible but cannot be replayed.

The shared controller also supports these commands:

```sh
listening-roomctl stations
listening-roomctl tune STATION_ID
listening-roomctl replay HISTORY_ID
listening-roomctl station-save '{"name":"Night drive","sources":[{"reference":{"provider":"jellyfin","id":"ITEM_ID","type":"Audio"}}]}'
listening-roomctl station-update '{"id":"STATION_ID","preferences":{"energy":"Low"}}'
listening-roomctl play-next '{"provider":"jellyfin","id":"ITEM_ID","type":"Audio"}'
```

The save/update/play-next arguments are JSON objects. IDs come from
`listening-roomctl stations`, the current session's Recent view, or the
Jellyfin library picker.

### YouTube: Play, Watch and Show as scene

Every YouTube result, suggestion and pasted YouTube link offers three ways to use it:

- **Play** sends the audio to the room's Music (Tune in only for a live stream).
- **Watch** opens the video with sound in its own player window: the `video` mode of
  the picture engine (`ambience.py`, app id `org.kotona.ListeningVideo`), placed like
  the scene. It is not a layer. The room's Music and Ambience pause the way Pause does
  and the Scene keeps going; Now shows "Watching · ‹title›" with Stop watching. When the
  video stops (Stop watching, the window closed, or the video ended, which closes the
  window), a toast offers Resume. The room never resumes on its own. Leaving the room
  closes the video too. With no room, Watch just plays.
- **Show as scene** puts the video in the picture layer, silently (the existing `link`
  destination `picture`). A saved or catalogue room is then marked Changed, with Save
  as my room.

The controller action is `video`: `{"action": "start", "url": "https://www.youtube.com/watch?v=…",
"name": "…"}` is queued behind other room changes (Pause and Leave cancel it while it
opens); `{"action": "stop"}` answers at once. Only public YouTube video links are
accepted. While the window is open, the snapshot carries `video` with `status`
(Opening, Playing or Paused), `name`, `url`, `started_at` and `room_paused`; the room's
resume offer then has `reason: "video"`. A failed start leaves `errors.video` and keeps
the room paused with its resume offer.

Before you type, the YouTube scope shows Your saved YouTube (stations and history with a
YouTube source, no network), "For ‹room›" (two short `ytsearch` queries built from the
active catalogue room's tags and subtitle) and "Live now" (a `results?search_query=`
search with YouTube's live filter). No account or cookies are used. Results are cached
in `$XDG_CACHE_HOME/listening-hub/youtube-shelves.json` with their fetch time: they show
at once, refresh off the GTK thread at most hourly (a failed refresh retries after ten
minutes and keeps the shelf), and entries older than a day are dropped. The cache is
re-validated on read: rows are rebuilt from 11-character video ids. Delete the file to
start fresh. Personalised recommendations need a YouTube account and are not offered.

## Recording metadata

Open **Metadata** beneath a selected Jellyfin track in the library view. Enrich
collects reported recording IDs plus existing AudioMuse tempo, RMS energy and
mood estimates. Enrich shown tracks and Enrich upcoming queue up to 20 jobs.
Library stations automatically refresh up to eight missing/stale candidates;
ordinary playback and Quiet remain independent of these jobs.

Match offers MusicBrainz text search or AcoustID audio fingerprints. Public
matching is explicit, and candidates need Confirm identity. Identity
confirmation is separate from reviewing audio. AcoustID setup stores an
application client key privately; MusicBrainz search does not need that key.
An existing `ACOUSTID_CLIENT_KEY` environment variable overrides the saved key.
Cancel stops the current metadata job, including stalled network work.

Analyze downloads the complete original and runs the packaged CPU
voice/instrumental models. Scores are estimates, not a probability that an
entire recording is voice-free. Analysis records the exact original SHA-256,
model checksums and preprocessing version. Historical AudioMuse analysis dates
and model revisions remain unknown; retrieval time is not an analysis date.
Weights are separately attributed under CC BY-NC-SA 4.0; see
`src/models/README.md`.

Review this exact recording is optional. Listen to the complete original,
choose instrumental/vocal/spoken/unknown, enter a reviewer name and acknowledge
the scope. Instrumental means no speaking, singing or vocal samples. The
controller computes the original hash independently. A changed or revoked
review cancels pending strict playback or stops an active strict track. These
actions never start music themselves. Strict players have private pre-launch
recovery markers: a restarted controller stops only those exact orphaned units,
while ordinary playback is preserved. Invalid or unreadable recovery data
blocks new entry until repaired; the marker is retained for retry. Original downloads are limited to
512 MiB, and direct classification to 20 minutes and a bounded execution time.

Useful CLI entry points:

```sh
listening-roomctl metadata-list
listening-roomctl metadata-enrich '{"reference":{"provider":"jellyfin","id":"TRACK_ID","type":"Audio"}}'
listening-roomctl metadata-match '{"reference":{"provider":"jellyfin","id":"TRACK_ID","type":"Audio"},"method":"musicbrainz"}'
listening-roomctl metadata-analyze '{"reference":{"provider":"jellyfin","id":"TRACK_ID","type":"Audio"}}'
listening-roomctl metadata-jobs
listening-roomctl metadata-cancel '{"job_id":"JOB_ID"}'
listening-roomctl metadata-export > recording-metadata.json
listening-roomctl metadata-import recording-metadata.json
```

Exported observations include provenance and the current review. Imports do not
trust reviews or hashes and do not overwrite existing local evidence. The
private SQLite store is under the controller's Listening Room state directory;
it is separate from saved room/station recipes. See
[the metadata design](../plans/listening-room-metadata.md) for trust boundaries.

## Rooms, Now and the mini player

On Rooms, choose a room and Enter room. Selection, filters, Surprise me and
pasted links preview choices; they do not start playback. Now shows the
current room and its controls. The mini player reduces the surface to room art,
name, the now-playing line, pause, room volume, timer and expand. Closing the window keeps
the session playing. Rooms and Listen return to source and room selection while the current
room stays active. Leave room fades and stops owned layers; Pause (Quiet)
pauses them immediately. The compact menu remains available through Tune in and
Super+Shift+M.

Mix (from Now) has Music, Ambience and Scene rows, with separate state and
controls (the controller still names the layers music, atmosphere and picture).
Choose an offline sound with Change… on Ambience, adjust its volume, and use
Window for outdoor sounds. Indoor sounds and Twitch do not expose that window control. Music and
Ambience have independent volume and mute. Scene supports Moving picture (off for
still artwork), Workspace 9, Here, On top and Small placement, an optional preferred
display (Preferences) and Try again. If the preferred display is missing, the
current workspace is used.

Save this room records the selected music, atmosphere, window setting,
picture/still/placement and output preferences as a personal room. My rooms
offers explicit re-entry. Reopening or restarting offers Return to ‹room›;
it never autoplays. Catalog and favorite sources are stored by reference.
Optional Dim the scene darkens only the owned picture over 30 minutes; it does not alter
display brightness or generate a new scene. Reduced motion prevents that
progression and makes new room entries use still artwork.

The Timer popover includes Break 10 min (the timed Quiet) and Wind down 30 min.
A timed Break offers a resume action when due; it does not resume automatically.
Wind down fades Music while Ambience continues. Cancel restores the prior music level.
Leave room fades both sound layers before stopping.
Quiet and Stop advance a cross-process epoch and serialize starts, so pending
work cannot restart sound afterward. Background audio replacement stages a
muted candidate; if loading, activation or persistence fails, the previous
player remains available. If cleanup of a paused retired player fails, its
warning appears beside Background audio; Stop retries cleanup.

Discovery preferences use authored room traits and recent room history. They
are not live speech/music detection or a guarantee about radio programming.
Picture and Background audio failures are reported independently beside those
layers.

## Desktop, MPRIS and phone controls

Home Manager declares an idle `listening-session.service` under the graphical
session. It owns a private Unix socket at
`$XDG_RUNTIME_DIR/listening-hub/session.sock`. GTK, room commands and Waybar
use that controller. MPRIS is `org.mpris.MediaPlayer2.ListeningRoom`; live
radio reports no track skip or seek, while supported library queues and
seekable items expose their actual capabilities. The hub excludes its own
MPRIS identity when discovering external players. Quiet and Stop bypass queued
discovery/load work and invalidate stale requests. Picture and audio
replacement starts are serialized; canceled loaders cannot stop a newer
player.

```sh
listening-roomctl enter midnight-library
listening-roomctl quiet 10
listening-roomctl resume
listening-roomctl save 'Rainy reading'
listening-roomctl return
listening-roomctl leave
listening-roomctl stop
listening-roomctl inspect
listening-roomctl menu
listening-roomctl volume        # print the room volume
listening-roomctl volume 60     # set it (0-100); +5 and -5 change it
listening-room --mini          # the mini player (--desk is the old name)
listening-roomctl popover      # the Waybar left-click controls
listening-roomctl status --follow  # Waybar: one line per status change (exec, no interval)
```

Room volume is one session multiplier (percent) over Music and Ambience. It
scales both without changing either layer's own level or mute, so their balance
is kept. It lives in the controller (`room_volume` in `inspect`, the
`room_volume` action with `value` or `delta`), is saved with a saved room and
the return offer, and is the value MPRIS `Volume` and `listening-roomctl volume`
read and set. Changing it never touches Quiet/Stop epochs or a queued start.
Controller snapshots also report `timers` (running Break, Wind down and Dim
timers as end times on the controller clock `now`) and `last_entered_at`.

The window (Rooms, Listen and Now with Mix, Timer and Output) is the only window:
`listening-room` opens it, and `--ui=next` is accepted and ignored. The classic window and
`--ui=classic` are removed (the launcher refuses it with exit status 2); the code is in git history,
before the commit that removed it. It sends no command
that starts or changes playback until you act: Return, Enter and Tune in start sound;
Pause is Quiet and Play is the existing resume. Space pauses or resumes, Esc closes Mix and
then goes back to browsing, Up and Down change room volume in Now and the mini player,
Ctrl+1/2/3 open Rooms, Listen and Now, Ctrl+M opens the mini player, Ctrl+, Preferences,
Ctrl+? the shortcuts. Its scene plays the room's reviewed loop, muted, only while the
window is visible (not minimized or suspended) and motion is allowed, otherwise still
art; GStreamer is required for the loop.

**Glance** (`next_mini.py`). One component, the mini player, shows room art, name, the
now-playing line, pause, room volume, timer and expand. Ctrl+M or Menu › Mini player opens
it in its own small window and leaves the main window alone; below 560 px wide the main
window shows it instead (AdwBreakpoint), and Expand (or widening the window) goes back.
`listening-room --mini` (also `--desk`) opens it; `--inside` opens Now. The window is a
single instance: a second `listening-room` with `--mini`, `--inside`, `--preview-link` or a
`listening-room://open` URI is delivered to the running window. `listening-roomctl popover`
(Waybar left-click) is the same component with Break 10 min, Wind down 30 min and Leave room;
it closes on Esc, its close control, Open the room window or a second Waybar click, and stays
open when it loses focus. Desk mode is retired. Menu › Other players… lists
players outside the room (browsers, Jellyfin, KDE Connect) with transport and volume, and
system volume with Mute.

Rooms (`next_rooms.py`, with its data rules in `next_rooms_model.py`) shows the
Return hero, mood chips (All, Yours, Focus, Reading, Late night, Lively, Something
new), Surprise me and a card grid with saved rooms first. A click or Space opens
the Room preview, which starts nothing; Enter room, Enter or double-click on a
card, and the toast's Back to the previous room are the only starts. Surprise me
only opens a preview. Save as my room… (menu and Mix) uses the existing `save`
command. A saved room's card menu renames it (the additive `rename` action: `id`
and `name`, which also moves the room's id with its name) or deletes it after a
confirmation. Scenes saved by the retired window (`scenes.json`, ids `user-…`)
appear under Yours and enter as a recipe; they are not editable from the card.
`tests/probes/listening-next-rooms.py` proves this and writes the O2 evidence
screenshots; `tests/test_listening_next_rooms.py` covers the rules.

Listen is search first. A search entry (Ctrl+F, or start typing) filters the
scopes All, Your stations, Radio, Library, Live channels and World radio. World
radio and library searches run off the GTK thread, can be stopped from the
section header, and are superseded by newer ones. A public URL typed or pasted
into the entry becomes a link result; Ctrl+V outside an entry, a dropped link,
Ctrl+L or the menu's "Add a link…" opens the link sheet without starting
anything. Shelves show Your stations (cards with Tune in and New station),
Recently played (Replay only where the history kept track references, else Tune
in, with History for the full list), Radio, Library and Live channels when their
provider has data, and a collapsed Open elsewhere section. Each row's menu holds
Play as ambience, Save as station, Favourite for radio, Open elsewhere and Show
as scene where the link supports a picture. A now-playing bar is pinned at the
bottom while a room is active.

A station page (opened from a card or the row menu) offers Tune in, Play as
ambience and, for library stations, the mood choices. Each choice maps onto the
existing `station_engine` values: Energy Calm/Any/Lively is Low/Any/High;
Familiarity Familiar/A mix/Discover is Familiar/Any/New; Voices Any/Fewer/
Instrumental only is Any/Likely instrumental/Strict instrumental; Over time
Steady/Wind down/Rise is `arc` none/wind_down/rise; When it runs out Stop/Repeat
is `end_policy`. Fewer shows for both `Less speech` and `Likely instrumental`
and keeps the stored value until another Voices choice is made, so the five speech
values the engine stores stay reachable there. Each change saves with
`station.save` and shows one toast with Undo; there is no Save button. The hint
under Voices appears only for the constrained value, and Instrumental only
counts the human-reviewed recordings from `metadata.list`. Fallbacks reorder
from the keyboard (Move up/down buttons) and by drag, and save as they move;
adding one uses `station.save`, removing one offers Undo. Show what's up next
stays collapsed until asked, and Forget what it learned… asks for confirmation
before `station.reset_memory`.

Preferences (Menu › Preferences…, or Ctrl+,) in the same window gathers the
setup surfaces: Connections, Sound and output, Scene and motion, Phone and
desktop, Recordings and About. Each connection shows what is actually known:
a connection is "connected" only after a successful check, a saved sign-in that
has not been checked says so, and an unchecked service says "Added · not checked
yet". Capability chips name what each service can do. Set up / Manage reuse the
existing native sign-in (Jellyfin username and password, Twitch device code),
Home Assistant's explicit scene link and the existing private credential files;
tokens are never shown again, and testing or saving a connection never starts
playback. Sound changes output, Test sound and the disconnect, call and
other-player policies; Scene changes placement, preferred display, moving
picture, reduced motion and Keep controls visible, which turns off Now's idle
fade. Phone and desktop installs KDE Connect room commands explicitly and
reports what the Waybar module and launcher entry can reach. Recordings holds
the AcoustID application key (an `ACOUSTID_CLIENT_KEY` override is shown as
such), bulk enrich for upcoming tracks and the prepared library queue, running
jobs with Cancel, and export or import of recording notes. Every change applies
at once through the existing settings commands.

Track details opens from a library track in Listen. It shows the recording's
reported identities, estimates and review with the scope statements the
retired metadata panel used, and offers Enrich, Match (MusicBrainz text search or an
AcoustID fingerprint) with Confirm identity, Analyze and Review this exact
recording. None of these actions starts music.

`tests/probes/listening-next-ui.py` exercises the window against a fake backend
on a private Xvfb and D-Bus with portals off and isolated `HOME`/`XDG_*`; run
it from `scripts/listening-dev.sh shell`. Its `--motion-probe` step also
measures the scene loop on the probe's software renderer (2026-11-17,
1440x810 at 24 fps, no DRI3). Measured: still art about 0.2 CPU s/s, the same
window with the loop about 1.2 CPU s/s, and decoding the loop alone at realtime
0.03 CPU s per second of playback. Decoding is therefore cheap; nearly all of
the in-window cost is software compositing of a full-bleed video, which a
GPU-composited desktop does not pay on the CPU. The loop stays the default for
that reason and because it pauses whenever the window is hidden. Use Still
artwork or reduced motion if it ever costs more than you want.

`tests/probes/listening-session-ui.py` (run by `scripts/listening-dev.sh probe OUTDIR`) is the
evidence probe: it captures the plan's screenshots (first run, return available, Rooms,
preview, Now on radio and on a library queue, Mix, layer failure, timer, Listen search,
station detail, link sheet, Connections, mini player, Waybar popover, 420 px mini layout, Other
players, reduced motion), and checks Glance, delivery of a second launch, the keyboard map and
shortcuts dialog, focus rings over the art, text contrast in all eight rooms, widths of 1200, 800
and 420 px, and reduced motion.

`tests/probes/listening-listen-ui.py` covers Listen against fixture World Radio,
library and Twitch providers: shelves, scopes, link results and the link sheet,
the launch URI, station autosave and Undo, fallback reordering (buttons and the
drop handler), History, New station, typing and Ctrl+F, accessible names and the
800 px layout. It logs every controller command and asserts that browsing,
searching, opening pages and menus send none.

Waybar shows the current room and active sound layers. Left-click opens the
mini controls (`listening-roomctl popover`); right-click opens the full window. Desktop launcher
actions expose Return to last room, Choose a room, Tune into a saved station, Pause the room and Mini player. Phone remote setup in
Connections lists reachable paired KDE Connect devices and explicitly installs
fixed room commands, preserving unrelated commands. MPRIS supplies normal
media controls where the paired client supports them. Setup does not pair a
phone or change firewall rules.

Drop a supported public link, paste one into Find & links or Connections, or
run `listening-roomctl link URL`. These actions preview the link without
playback. The registered desktop URI is
`listening-room://open?url=<percent-encoded-public-url>`. A browser bookmark
can explicitly send its current page with:

```js
javascript:location.href='listening-room://open?url='+encodeURIComponent(location.href)
```

Supported destinations are shown for each link. Search pages open in their
original service. No browser account or cookies are imported. A URI or
`--preview-link` request, on the first launch or delivered to the running window,
opens Listen's link sheet (nothing plays until a destination is chosen).

### Push updates (`subscribe`)

Clients learn about controller changes when they happen. The `subscribe` action
streams one JSON line per change over the same private socket, with the same peer,
permission and size checks as every request. `inspect` reports the protocol
`version` (2 since `subscribe`); the actions, whether each runs at once or waits
behind the generation-checked queue, and their argument types are declared once in
the `room_session` action registry. Unknown actions and wrongly typed arguments
fail at once with a plain error.

- Request: `{"action": "subscribe", "args": {"fields": ["active", "layers"]}}`;
  `fields` (up to 64 top-level snapshot keys) is optional and limits both the
  content and the lines to changes in those keys.
- Each line: `{"ok": true, "result": {"event": "snapshot", "version": 2, "session": S,
  "revision": N, "heartbeat": 15, "snapshot": {...}}}`. The first line is the
  current snapshot. A revision exists only when the snapshot changed (the clock
  field `now` alone is not a change); revisions increase within one controller
  `session`, and a gap means changes were coalesced or filtered.
- Idle: a `{"event": "heartbeat"}` line every 15 seconds and nothing else; an
  idle subscriber sleeps until a change or its heartbeat.
- Errors are an `{"ok": false, "error": ...}` line, then the stream closes.
- At most 64 subscribers. A subscriber that does not take a line within 5 seconds
  is dropped, and one that hangs up is released at once; neither delays the
  controller or other clients, which always receive the latest snapshot.

The client API is in `session_client.py`:

```python
import session_client

for update in session_client.Subscription(fields=['active', 'layers']):
    if update.connected:
        show(update.snapshot, update.revision)
    else:
        show_offline(update.error)

subscription = session_client.subscribe(callback)   # runs on a daemon thread
subscription.close()
```

An `Update` is `(snapshot, revision, session, connected, error)`. A
`connected=False` update arrives once per outage; the client then reconnects with
jittered backoff (0.25 s up to 10 s) and yields a fresh snapshot, with a new
`session` after a controller restart. A silent connection is dropped after three
missed heartbeats. It never starts playback: by default it only waits for the
controller, and `start=True` calls `ensure_service`, which starts only idle
infrastructure. The callback form runs on its own thread, so a GTK client hands
updates to its main loop with `GLib.idle_add`. Clients that poll `inspect` keep
working unchanged.

## Audio setup

The output selector remembers a stable sink name and routes owned room
streams. It never changes the system default or moves unrelated applications.
New owned players select a configured available output before becoming
audible. An unavailable preferred output with pause-on-disconnect enabled
prevents a new start. The service checks device disappearance and offers
explicit recovery; polling does not guarantee zero latency after hardware
removal. Call ducking is optional and detects only applications that publish
Communication or Phone stream roles. Manual volume changes during a call are
preserved. Test sound plays one quiet second only after the user presses its
button.

The six offline atmospheres are original procedural 40-second sound beds: rain
on glass, fireplace, distant thunder, forest wind, train carriage and a
synthetic nonverbal café murmur. They use no external recordings or sampled
voices. `render_atmospheres.py` creates WAV files and CC0 provenance/hashes
during packaging. Each bed is built from the events that define it (droplets,
crackles, thunder rolls, gusts and leaf rustle, paired rail-joint clacks,
formant-shaped babble and clinks) rather than from filtered noise, and every bed
is scaled to the same perceived loudness, measured as A-weighted RMS (31 dB below
full scale; raw RMS left a 17 dB A-weighted spread, with thunder far quieter than
rain on ordinary speakers). The beds stay within 1.5 dB of each other on that
measure, with peaks softly limited below 0.65. Thunder's low rolls carry little
A-weighted level, so holding it level with the others costs raw headroom, which is
why the whole set sits this low and every raw RMS stays below -12 dBFS. Distant
thunder keeps its rain audible between the rolls (sparse droplets and a swelling
wash, about 7 dB below the rain-on-glass bed in 1.5-9 kHz, over a faint continuous
rumble), so its 1 s level never falls below -40 dBFS.
`tests/test_listening_atmosphere_character.py`
measures spectral flatness and tilt, onset rate, low-frequency rumble events,
gust-rate modulation, clack periodicity and voiced formant texture, and checks
that the six beds differ, loop without a click and render byte-identically. Those
are objective checks, not a judgement of how real the sounds are; procedural
synthesis stays an approximation of recordings.

## Optional online connections

Connections (Preferences › Connections) keeps public endpoints/settings separate from private credentials.
Each integration has explicit Save and Test actions; connection setup does not
start music or change lights. Discovery runs off the GTK thread. Errors are
shown beside the operation and local playback works without these services.

- **Jellyfin:** Listen › Library has a kind choice (Albums, Playlists, Recently
  added, Tracks); a row's menu › Show tracks lists an album's or playlist's tracks,
  and track rows carry Play next, Track details and Make an AudioMuse station.
  Load artwork, inspect a prepared queue and play through Main audio. Results
  are bounded (up to 60 album tracks or 200 playlist tracks). Library
  references support room restoration and queue navigation. A session-owned
  loopback-only relay adds authentication to the configured server; service
  credentials do not reach mpv or cross-origin redirects. Connections supports
  native username/password sign-in. Feishin remains available through the
  existing launcher and Other apps controls.
- **Music Assistant:** when configured, Listen › Speakers loads its players,
  browses playlists/tracks/albums (search reaches the providers; an optional
  provider narrows it), plays a selected item on the chosen speaker, starts a
  radio mix from the row menu and, from a speaker's menu, moves another speaker's
  queue to it. Browsing and loading speakers send nothing; only the play and move
  actions do. Grouped players resolve
  their active queue. Queue transfer moves remote music; it does not transfer
  the desktop's composed Main audio + Background audio mix or Picture.
- **AudioMuse AI:** optional sonic programming starts from a selected Jellyfin
  track (Listen › Library › a track's menu › Make an AudioMuse station); generated item IDs are verified against the connected Jellyfin
  library. The connection check reads the pinned v0.8.8 `/api/config` shape.
  It checks the web process only, not worker readiness or analysis. Previously
  generated Jellyfin playlists remain browseable in Jellyfin.
- **Twitch:** curated Music and ASMR pools work without API access and are edited
  in Preferences › Connections › Twitch › Channels you keep. Device-code sign-in
  supports Review my follows (same page): it reads your follows once, when you
  press Review, and adds only the channels you tick to Music or ASMR. Live
  channels search covers both styles. Live discovery/playback depends
  on Twitch and the local resolver.
- **Home Assistant:** load scenes and explicitly link a scene to a room.
  Reported affected entities are snapshotted before activation; newly affected
  entities across room changes receive additional snapshots. Stop/Leave
  restores snapshots in reverse order. A scene without known entities cannot
  be linked through the UI.
- **ListenBrainz and local history:** Remember playing stores reported metadata,
  station, time and optional notes locally, retaining uncertainty. Listen ›
  Remembered lists them, with Find in Jellyfin and Open in YouTube Music for
  discovery. History and recommendations load only when you press their buttons; recommendation recording IDs link to recording details.
  Automatic scrobbling is off. Submission enforces the service listening
  threshold.

At the implementation infrastructure probe, Jellyfin had one desired and one
ready deployment; AudioMuse's Flask web and worker deployments were both 0/0;
no matching Music Assistant deployment was found. Desktop Jellyfin, Music
Assistant and Home Assistant endpoint settings were unconfigured, and no real
authenticated playback was attempted. These probe results do not establish
working desktop connections.

## Development loop

`scripts/listening-dev.sh test [SUITE...]` runs the package's test suites
against the checkout in about 40 seconds, with isolated HOME/XDG state, and
validates the cached scene render. `scripts/listening-dev.sh probe [OUTDIR]`
runs real GTK on a private Xvfb display and D-Bus session, writing screenshots.
It disables portals and uses its own runtime directory, so it never reaches the
live compositor or session. Both reuse `.#listening-hub.assets` and do not rebuild
the package. `nix build .#listening-hub` remains the full check.
