# Listening hub

The listening hub is a keyboard-driven catalogue for radio, browser discovery,
and local media apps. Its starter catalogue is in
[`src/catalogue.json`](../../src/catalogue.json).

## Open and control playback

For the Listening Room 2 inside/desk interface, complete saved rooms, offline atmosphere, desktop media service and optional online systems, see [session operations](listening-room-session.md). The compact Tune in menu remains available.

- Open the menu with **Super+Shift+M**.
- Open the optional native **Listening room** with **Super+Ctrl+M**, its app
  launcher entry, right-click on **Tune in**, or the compact menu's first row.
- Use `listening-hub menu` to open it from a terminal.
- Use `listening-hub status` to inspect the active players and selected target.
- Right-click the now-playing label, or run `listening-hub select-player`, to
  choose which player the label, **Super+M**, and media keys control. Left-click
  toggles playback; middle-click opens Tune in.
- Use `listening-hub play <source-id>` to open a catalogue entry.
- Use `listening-hub toggle` to pause or resume the selected playback target.
- Use `listening-hub players` to list available player backends and
  `listening-hub doctor` to inspect playback prerequisites.
- **Super+M** and the media keys control the selected listening-hub playback
  target.

Scroll over the now-playing label to adjust the selected player's volume in
5% steps. The compact menu also has volume and mute actions. In Listening room,
**Player volume** and **System volume** are separate: the first controls the
chosen player, the second the desktop's default audio output. Unsupported
player volume is disabled; system volume remains available. Explicit player
selection also applies to queued volume changes, so changing the selector does
not redirect an earlier slider adjustment to another player.

Radio and other apps may overlap by default. Starting radio leaves browser and
Jellyfin playback alone. The hub has one native radio player: choosing another
station replaces that station. Pause either the radio or another app by selecting
it and using the playback toggle.

The menu row **When starting radio** toggles a persistent preference. Enable it
to pause other playing MPRIS players only after the new stream has loaded.
Change it from the terminal with `listening-hub config pause-others on` or
`listening-hub config pause-others off` (the default). Opening a browser page or
Jellyfin does not pause anything; those applications retain their own controls.

The menu is idle until you choose something. Installing or signing in does not
automatically start audio.

## Saved default and listening scenes

The original compact interface is preserved at Git tag
`listening-hub-compact-default` (commit `102e6fa`). The toolbar and **Super+Shift+M**
remain the default interface; the richer window is an opt-in companion.

The first use of the richer controls saves a write-once **default** settings
profile. **Restore default** restores its radio-start preference and public
favorites without restarting, pausing or stopping current playback. Settings
profiles do not change system volume, browser sessions or transient player IDs.
Existing favorites containing access tokens are kept separately from snapshots.

```sh
listening-hub profile save evening
listening-hub profile restore default
listening-hub profile save evening --force
```

**Scenes** are explicit listening sessions: the built-in Focus, Night drive and
Dark academia scenes combine a radio source with a silent offline view on
workspace 9; Finnish discovery opens YleX plus the view. Nothing starts on login.
**Save current scene** remembers the selected radio station, its player volume,
and an available vista preset and placement. Save a directory result as a
favorite before creating a scene from it.

```sh
listening-hub scene save "Late reading"
listening-hub scene play dark-academia
```

Listening room opens on **Rooms**: choose a mood from the left rail to preview
its sound and view. Selection and **Surprise me** never start playback. Check
whether to include the silent view, then choose **Enter room**. The recipe and
launch controls remain visible when the preview scrolls, and the last entered
room is selected on reopening without replaying it.

Rooms cover Midnight library, Night drive, Northern quiet, Velvet circuit,
After hours, Atomic lounge, Finnish discoveries and Wizarding Valley. Each has
its own authored artwork, matching preview, subtle offline loop and accent.
**Still artwork** opens the full plate without motion when entering a room.
It does not change an already open window. Wizarding Valley pairs the original
valley artwork with Radio Rivendell; its fantasy programming can become dramatic.
The gentler YouTube Music discovery option is under Listen. The visual/audio
plan and asset provenance are in [the room visual plan](../plans/listening-room-visuals.md).
All eight built-in rooms now start their audio directly, including YleX in
Finnish discoveries. Browser discovery links in Listen still open their own player. If sound starts but its view fails,
the message describes that partial result and keeps the audio controls available.

**Quiet** pauses owned radio/video, the silent view, and the explicitly selected
app. It does not adopt a new browser that appears while the action is queued.
**Stop room** stops only the hub's own players. The compact player strip keeps
source volume and transport available; **Mixer** expands system audio controls.

The **Listen**, **Views**, and **Scenes & settings** tabs remain available.
**Ctrl+1/2/3** switches tabs, **Ctrl+F** finds sources, **Ctrl+L** focuses the
video URL, **Ctrl+4** returns to Rooms, and **Ctrl+W** closes the control window
without stopping playback. A native `preview-room` action accepts a recipe ID
for preview-only automation; it never enters the room.

## Movable vistas and pasted video URLs

**Views** opens an ordinary mpv window, initially floating on **workspace 9**.
**Super+9** visits it. **Bring here** moves only that view to the current
workspace; **Pin / unpin** makes it follow workspaces. The normal Hyprland
move/resize/tile controls still apply. The helper uses the pinned compositor's
Lua dispatch API and addresses only windows with its own application identities.

- **Open silent vista** loops the selected landscape without audio. Silent
  ambience does not inhibit idle blanking or become the music-control target.
- Paste a public YouTube video URL and choose **Play YouTube audio** for audio
  playback in the hub, with ordinary player volume and radio-start overlap behavior.
  Search and playlist pages remain discovery links. Public video restrictions can
  prevent playback; the app does not sign into an account or read browser cookies.
- Paste a public YouTube or direct video URL and choose **Silent ambience** or
  **Play video with sound**. The latter has separate volume/mute and becomes an
  explicit player target only after loading succeeds.
- Radio, silent ambience and sound-enabled video use independent player units.
  Closing one does not close the others. The radio-start pause preference still
  applies when starting radio; opening a video preserves the user's overlap
  choice and never implicitly pauses other audio.

The two offline lake loops are generated during packaging and need no network.
Norway and Nordic long-form films use the packaged yt-dlp resolver, with video
selection capped at 1080p. YouTube availability can change; a failed URL reports
an error and leaves the audio player alone. No cookies are extracted or stored.
An absolute local video path also works from the CLI.

```sh
listening-hub ambience start northern-lake
listening-hub ambience here
listening-hub ambience pin
listening-hub ambience stop
listening-hub video start 'https://www.youtube.com/watch?v=VaLgmb5aGjo'
listening-hub video toggle
listening-hub video stop
```

Set `LISTENING_HUB_WIZARD_VIDEO` to offer an existing local Wizard Valley clip
and `LISTENING_HUB_WIZARD_ARTWORK` to offer the original artwork as a still view.
Both are read in place; the hub does not render or copy them.

## Stations, favorites, and discovery

Type a station, mood, group or tag in Tune in to search the entire catalogue.
**Search world radio** accepts a station name, `country:FI`, `tag:electro swing`
or `language:Finnish`. **Discover Finnish radio stations** runs the country
search directly. Choose **Save favorite** after selecting a result; favorites
appear in Tune in and can be removed from the station's action menu. Use
**Custom radio URL** for an official stream or playlist of your own.

The shipped JSON catalogue provides the starter sources. Personal settings,
search results and favorites live in the hub's user state under
`$XDG_STATE_HOME/listening-hub/` (normally
`~/.local/state/listening-hub/`). Keep personal additions there so package
updates do not replace them. Use an official published stream or playlist URL
for a radio entry; browser services such as Yle Areena and Radio Garden should
remain browser links when they resolve streams dynamically.

The catalogue includes direct official playlists for TruckersFM and SomaFM,
plus laut.fm's stable station routes for Electro Swing and Nightcore. Yle links
open official pages that resolve their live players; rights, sign-in, or site
changes can affect browser playback. YouTube Music entries open searches in the
user's existing service. Twitch is a browser destination. These web services
are not extracted or downloaded by the hub.

**Dark academia piano** tunes to laut.fm Relaxing Piano, a neoclassical reading
station. A second YouTube Music discovery entry explores darker piano and cello
mixes. Finnish discovery combines native Radio Browser station searches, Yle
pages, and a Finnish new-music search in YouTube Music.

**Jellyfin Music (Feishin)** opens the packaged music client with server type
Jellyfin. If the package was built with a server address (the `jellyfinUrl`
override, see the README), that address is prefilled; otherwise add your server
in the app. Sign in there on first launch. The wrapper uses Feishin's supported `SERVER_TYPE`, `SERVER_NAME`
and `SERVER_URL` environment variables; it leaves server locking disabled so
existing server profiles remain editable.
**Jellyfin Desktop** remains
available for the existing media client. The hub stores neither Jellyfin
credentials nor library configuration. Once the client exposes playback over
MPRIS it appears in **Select player**, alongside browser and remote players.

Browser audio may publish incomplete media metadata or MPRIS controls, depending
on the browser and site. Native radio playback is the predictable choice when
you want the hub's selected-player toggle and media keys to control one stream.

## Vintage and atompunk listening

The vintage entries are era-style inspiration: Radio Dismuke describes its
programming as popular music and jazz from 1925–1935, and the YouTube Music
search looks for 1940s swing and jazz. SomaFM Secret Agent adds a cinematic
retro-spy mood.

## Packaging and validation

`nix build .#listening-hub` builds the controller, GTK window, offline loops, catalogue and its hermetic
backend tests. `nix flake check` includes the same package. The README shows how to
add the package, Waybar module and launchers to a NixOS or Home Manager setup. There is
no separate MPD daemon to enable: browser players use MPRIS and the native radio
adapter uses one user-owned mpv service and private IPC socket. The service is
created only when a station is chosen.

Video units are also created only on explicit launch and stop with the graphical
session. A private socket per player keeps audio, video and ambience controls
independent. `listening-hub inspect` prints the richer control snapshot, including
volume availability, profiles and scene choices.

For a real desktop graphics check, build `.#listening-hub`, then run
`python3 tests/probes/listening-hub-views.py /nix/store/…-listening-hub-1.1.0`
from the graphical session. `CHECK_YOUTUBE=1` also verifies both public YouTube
modes; test audio is routed to null output. The probe uses separate unit names,
sockets and state, leaving normal playback alone. The OpenGL Wayland renderer
is intentional: the default gpu-next path hit a colour-management surface error
on the machine this was developed on.

Run `listening-hub doctor` for dependency paths and current pause policy. If a
station fails, choose another or use its website; a failed load leaves other
players running. With no active media, **Super+M** opens Tune in. Browser tabs
may share one MPRIS identity, so separate control of every tab depends on the
browser. Remote phone players exposed through KDE Connect can also appear in
the player selector.

## Individual scene quality sessions

Each room has a separate tuning JSON in `src/scene_tuning/`.
Final loops use 1440×810, 24 fps and a 20-second
period; full-resolution still artwork remains available. Scene configuration and
reviews are versioned; rendered candidates are local build output.

YleX, Yle Klassinen and Radio Dismuke now use direct streams. The Listen list
keeps a separate **Website** action for radio sources. Playback failures never
launch a browser automatically. [Source evidence](listening-direct-sources.md)
records current endpoints and verification limits.


## World Radio, Channels and layer controls

World Radio is inside the native room (Ctrl+5). Combine country, language,
genre and name filters, or choose Surprise me to preview a station. Search
never starts sound. Play sends the selected station to the existing radio
player; its star saves a favorite in Listen. The directory is community
maintained and a listed endpoint can fail or have inaccurate metadata.
Radio Garden remains an optional globe link.

Channels (Ctrl+6) provides Twitch Music and ASMR pools, explicit discovery,
shuffle previews and reviewed follow imports. Choose Music to replace the
primary radio, Atmosphere to keep it playing under an independent audio
layer, or Video for a separate sound-enabled window on workspace 9. Mixer
holds Atmosphere volume, mute, pause and stop alongside system volume; the
current-player strip controls the selected music or browser player. Both owned
audio lanes and the video window retain their volume and mute across source
changes. Quiet and Stop room also include Atmosphere. Starting Music respects
the configurable radio overlap preference. See [Twitch setup and limits](listening-twitch-layers.md).

Silent room windows hide mpv's controller, OSD and cursor, and request no
client decorations. Hyprland may retain its own window border. PiP brings
an owned picture to the current workspace as a movable, pinned 640×360 window;
Bring here or Workspace 9 removes its pin. Ordinary sound-enabled videos keep
their normal player controls.

`tests/probes/listening-hub-mix.py` checks real systemd/mpv replacement,
independent layers, retained levels and unchanged master volume with local HTTP
fixtures and null audio. It does not establish Twitch service compatibility.
See [scene motion passes](../scene-reviews/scene-pass-review.md) for the per-room
weather, light, fire, steam and water decisions and remaining visual limits.
