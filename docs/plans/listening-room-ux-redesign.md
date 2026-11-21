# Listening Room: experience redesign

Planned 2026-11-17. Scope is the GTK presentation layer and two small controller
additions. The session controller, MPRIS, `listening-roomctl`, Waybar data and every
session guarantee stay as they are. 
This plan replaces one clause of [personal radio](listening-room-modern-radio.md):
"Main navigation is Listen, Room and Recent" becomes **Rooms** and **Listen**, with
**Now** as the room you are in. Every other clause of that contract still applies.

## Why

Evidence: the UI probe capture of 2026-11-15 (Inside view, reduced motion) and the
current `listening_room.py` / `room_experience.py`.

- **The engine's structure is the interface.** Inside shows 32 interactive controls
  at rest: four navigation buttons, Return and Desk, a name entry and Save, five
  transport buttons, Session options, and a mixer with Main audio, Background audio and
  Picture rows (play, slider, Mute, Stop, Retry, Still, Workspace 9, Retry picture,
  Close). The product promise is "keeps going with little attention". The screen asks
  you to operate a mixing desk.
- **Controls are duplicated and overlap.** One screen has three play/pause controls.
  Quiet, Leave room, Stop and Close all end sound in different ways. Return to last room
  stays visible while you are in that room. Refresh duplicates a 0.9 s poll, and the
  status bar repeats "Current room".
- **Navigation hides where you spend time.** It has three buttons plus a menu of five
  more pages, and two of those pages are only redirect buttons (Channels, Find & links).
  Inside is not a destination. Desk resizes the main window. Ctrl+1 to Ctrl+6 open pages
  that the visible navigation does not show.
- **The art, the strongest asset, gets about 11 % of the window.** It is letterboxed
  under a heading, although every scene was reviewed frame by frame.
- **Disclaimers are used as UI.** True statements appear in the wrong place: "Room
  descriptions are editorial…", "Browsing does not start playback", and paragraphs of
  caveats in station preferences. The behaviour should carry the guarantee. Copy belongs
  only where a choice has consequences.
- **Modes silently change meaning.** "Send audio to: Main / Background" changes what
  every Tune in does.
- **There is no hierarchy.** Buttons are uniformly grey, with no primary action per
  screen. Sliders show raw numbers, and the play state is spelled "Play / pause".
  Preferences need an explicit Save, and a "Name this mix" entry is always visible.

Keep: inert browsing and explicit Enter / Tune in, no autoplay, per-layer failure
isolation, room accent tinting and serif room names, saved rooms, reduced motion,
Waybar / MPRIS / KDE Connect, and honest capability statements.

## Benchmarks: what to borrow

- Portal (immersive environments): the scene is the screen, chrome recedes, timers
  are first-class.
- Apple Music / Spotify: a persistent now-playing bar that expands into the full
  player, and output selection behind one icon.
- Endel / Brain.fm: mood-first entry, one action to sound, wind-down timers.
- GNOME Shortwave and Blanket: native GTK/libadwaita radio browsing, and an ambient
  mixer that shows only a name and a slider per sound.
- myNoise / Noisli: saved mixes as a first-class "yours".

## Principles

1. **One action to sound.** Returning to the last room is one activation. A new room is two.
2. **The room is the interface.** Full-bleed scene. Controls float over it and recede.
3. **Calm by default, detail on request.** There are four levels: Glance (mini, Waybar,
   phone), Room (Now), Tune (Mix, station, preview) and Setup (Preferences).
4. **Speak in the listener's words:** Music, Ambience, Scene, Pause, Break, Wind down.
5. **Behaviour carries the guarantees.** Copy appears only where a choice has consequences.
6. **Recover in place.** A failing layer never stops the room, and the fix is one click
   from the message.
7. **One primary action per screen**, drawn in the room accent.

## Information architecture

```text
Header (all screens): [Rooms | Listen]            Output · Timer · Menu
Rooms
  Return hero (last room)  |  first run: "Pick a room to begin"
  Mood filters · Surprise me
  Yours (saved rooms) + catalogue grid
  └ Room preview (pushed page) → Enter room → Now
Listen
  Search (also accepts a pasted link) · scopes: Your stations, Radio, Library,
    Live channels, World radio
  Your stations → Station detail
  Recently played → History
  Radio · Library · Live channels · Open elsewhere
Now (the room you are in, not a tab)
  reached from the now-playing bar, Return, Enter, or launch while playing
  Mix sheet · Save as my room
Menu: Save as my room… · Add a link… · Other players… · Mini player ·
      Preferences · Keyboard shortcuts · About
Preferences: Sound and output · Scene and motion · Connections ·
             Phone and desktop · Recordings · About
Glance: mini player (adaptive) · Waybar popover · Tune in menu · MPRIS · KDE Connect
```

Where today's surfaces go:

| Today | Redesign |
| --- | --- |
| Room page (rail, hero, checkboxes, four buttons) | Rooms grid, then Room preview |
| Inside | Now |
| Desk | Mini player: Ctrl+M, or automatic below 560 px width |
| Always-visible mixer | Mix sheet from Now |
| Listen page with destination dropdown | Listen search and shelves. Each item has Tune in, and Play as ambience in its menu |
| Recent | Rooms › Return hero; Listen › Recently played and History |
| Station preferences expander | Station detail page |
| Atmosphere, output and room setup | Mix › Ambience › Change; header Output popover; defaults in Preferences |
| Other apps and system volume | Menu › Other players…; the Output popover includes system volume |
| Views (vista, video URL, placement) | Scene row in Mix; video links go through Add a link |
| Scenes & settings | Saved scenes become My rooms. Settings profiles are retired. Pause other players moves to Preferences › Sound |
| World Radio, Channels, Find & links pages | Listen search scopes and the Live channels shelf; links paste or drop anywhere |
| Connections & library | Preferences › Connections; browsing moves to Listen › Library |
| Metadata panel (enrich, match, analyze, review) | Track details dialog from a library track's menu; Preferences › Recordings for keys and bulk jobs |
| Refresh, status bar | Removed. Toasts confirm actions and banners report problems |

## Screens

**Now.** Full-bleed room scene, with scrims behind the text only. Bottom left: a source
chip (Live radio / Library / Live channel), the room name in serif, and the now-playing
line (programme or track · source). The bottom row has three parts: Mix on the left, with
a summary of the active layers; play/pause in the centre; room volume on the right. Skip
and More / Less like this appear only for sources that support them. The header shows
back to Rooms, Output, Timer and Menu. After 4 s without input the controls and cursor
fade out, and any input brings them back. This does not happen while a banner is showing.

**Mix (sheet over Now).** One row per layer: Music, Ambience, Scene. Each row has an icon
tile, the source and its state, and one contextual control. That control is Change… for
Music, Window (Closed to Open) for outdoor Ambience, and placement (Workspace 9, Here, On
top, Small) for Scene. Music and Ambience rows also have a level slider; the Scene row has
a Moving picture switch. Each row ends with Mute or Close and a menu with Next track, Try
again, Stop and Change. Save as my room… opens a dialog with the name prefilled and a list
of what will be saved.

**Rooms.** Return hero shows the last room's art, when it was last visited, and one
primary button, "Return to ‹room›". Below it: mood chips (All, Yours, Focus, Reading, Late
night, Lively, Something new), then Surprise me, then a 16:9 card grid. Saved rooms come
first and carry a Yours tag. The active room's card shows a Playing marker.

**Room preview.** A large scene preview: the muted loop when motion is allowed, still art
otherwise. Then the name, subtitle, and two sentences of description. "What you'll hear"
lists Music (with "programming varies" for live radio), Scene (with a toggle) and
Ambience. Enter room is the single primary action. A short note says what happens to the
current room (it fades out; other apps keep playing). Double-click or Enter on a card in
the grid enters directly, which is still an explicit action.

**Listen.** Search comes first (Ctrl+F; a pasted URL becomes a link result), with scope
chips. Shelves show Your stations (cards with Tune in), Recently played (Replay only when
the history has track references, otherwise Tune in), Radio, Library and Live channels.
A collapsed Open elsewhere section comes last. Each row has an art tile, a name, one
descriptor line and Tune in. Its menu holds Play as ambience, Show as scene, Save as
station, Favourite and Open elsewhere. A now-playing bar is pinned at the bottom.

**Station detail.** Name, origin, Tune in, Play as ambience. The Mood controls are
segmented choices that save automatically:
Energy (Calm · Any · Lively), Familiarity (Familiar · A mix · Discover),
Voices (Any · Fewer · Instrumental only), Over time (Steady · Wind down · Rise) with
Hold this mood, and When it runs out (Stop · Repeat). A hint appears only for the
selected constrained value. For example, Instrumental only shows how many reviewed
recordings are available and a link to review more. The page also has: an ordered,
draggable "If it can't play, try" list; "Show what's up next" (only when asked); and
"What it has learned" with a Forget… action that needs confirmation.

**Link sheet.** Paste, drop, Ctrl+L, or a `listening-room://open` URI opens it. It
previews the link and offers the destinations the link supports: Play, Play as ambience,
Show as scene, Open elsewhere. Nothing plays until you choose one.

**Preferences.** An AdwPreferencesDialog with these sections:

- **Connections**: status, capability chips (Browse · Plays here · Plays on speakers ·
  Open elsewhere) and Set up / Manage.
- **Sound and output**: output, Pause if it disconnects, Lower during calls, Pause other
  players.
- **Scene and motion**: default placement, display, Moving picture, reduced motion,
  Keep controls visible.
- **Phone and desktop**: KDE Connect commands, Waybar, launcher.
- **Recordings**: AcoustID key, bulk enrich, export/import.

**Mini player and Waybar.** Room art, name, now-playing line, pause, room volume, timer
and expand. The Waybar left-click popover uses the same component, with Break 10 min,
Wind down 30 min and Leave room.

**Timer popover.** Break for 10 min (the existing timed Quiet, which offers resume and
never resumes on its own), Wind down over 30 min, Dim the scene over 30 min, and Cancel.
While a timer runs, the header shows it as a chip, for example "Wind down · 24 min".

**Output popover.** Owned-stream output devices, Test sound, and system volume.

## Interaction rules

- **Launch.** If a room is playing, open in Now. If a last room exists, open Rooms with
  the Return hero. On first run, open Rooms. The app never autoplays.
- **Browsing stays inert.** Previews are muted video; there is no audio preview.
  Filtering, searching, selecting, pasting and dropping links start nothing.
- **Pause** in Now is the existing Quiet: it pauses all owned layers at once, and Play
  is the existing resume. **Leave room** (menu, mini, Waybar) keeps the existing fade
  and stop. Entering another room fades the current one out first. A toast offers
  "Back to ‹previous room›".
- **Hide or disable.** Hide a control when the content can never support it (skip on
  live radio, Window on indoor ambience). Disable it only when it is temporarily
  unavailable, with a tooltip giving the reason.
- **Problems.** A Now banner names the failing layer and says what keeps playing, with
  Try again and Choose another. The Mix button shows a marker. Recovery and staging
  rules are unchanged.
- **Confirmations.** Toasts, with Undo where the action can be reversed (a preference
  change, removing a fallback, deleting a station).
- **Accent.** The room accent colours the primary button, slider fills and chips.
  Accent text is used only where it reaches 4.5:1 contrast.
- **Keyboard.** Space pauses or resumes the room; Ctrl+1/2/3 open Rooms, Listen and Now;
  Ctrl+F searches; Ctrl+L adds a link (Ctrl+V outside entries does the same); Ctrl+M
  opens the mini player; Ctrl+, opens Preferences; Esc closes a sheet, then returns to
  browsing; ↑ and ↓ change room volume in Now; Ctrl+? shows shortcuts.
- **Adaptive widths.** At 1000 px and wider: four-column grid and a floating Mix sheet.
  From 560 to 999 px: two or three columns and a full-width sheet. Below 560 px: the
  mini layout.

## Visual system

- **Toolkit:** libadwaita on GTK 4, with the dark scheme forced. Use AdwNavigationView,
  AdwViewSwitcher, AdwBreakpoint, AdwToastOverlay, AdwBanner, AdwPreferencesDialog,
  segmented toggle groups, and a bottom sheet or overlay for Mix.
- **Type:** Inter / Adwaita Sans for the UI. Room names use one packaged OFL literary
  serif; the mockups use Newsreader.
- **Colour:** neutral dark greys (window `#17171a`, header `#1f1f23`). Each room's
  catalogue `accent` provides a fill colour and a light tint for text on dark.
- **Imagery:** cover fit with a per-room focal point from `scene_tuning`; scrims cover
  the top 140 px and the bottom 40 to 55 %.
- **Shape:** 12 to 16 px radii, pill-shaped floating controls, targets of at least 44 px.
- **Motion:** sheets take 200 ms ease-out and room crossfades take 700 to 900 ms. With
  reduced motion there is no animation, but audio fades remain.

## Controller additions

1. **Room volume.** A session multiplier applied to Music and Ambience that preserves
   their balance. Saved rooms store it, and `listening-roomctl`, MPRIS Volume and Waybar
   share it. Per-layer level and mute stay where they are.
2. **Snapshot fields the UI needs**, where they are missing: the room accent and focal
   point, per-layer capability flags (skip, outdoor; `can_next` and `outdoor` already
   exist), and the last-visited time for the Return hero.

Nothing changes in epochs and start serialization, candidate staging and fallback,
credential storage, MPRIS identity, or the CLI verbs.

## Copy

| Current | Redesign |
| --- | --- |
| Main audio / Background audio / Picture | Music / Ambience / Scene |
| Quiet · Resume interrupted room | Pause · Play |
| Quiet for 10 minutes | Break · 10 min |
| Stop room, Close | Leave room (fades out) |
| Workspace 9 / Here / Pinned / Small picture | Workspace 9 / Here / On top / Small |
| Still artwork | Moving picture (off) |
| Retry, Retry picture | Try again (from the banner or a row menu) |
| Dusk over 30 minutes | Dim the scene over 30 min |
| Return to last room | Return to ‹room name› |
| Approved fallback sources | If it can't play, try |
| Reset station memory | Forget what it learned… |
| Hold mood | Hold this mood |
| Save this room / Name this mix | Save as my room… |
| Send audio to Main / Background | Tune in · Play as ambience (per item) |

## Retired

Each retired surface has its reason:

- **Refresh:** state is live.
- **Status bar:** replaced by toasts and banners.
- **Desk mode:** replaced by the mini player.
- **Channels and Find & links stub pages:** they only redirected.
- **Global destination dropdown:** a hidden mode.
- **Settings profiles:** local state on 2026-11-17 holds only the default profile;
  favourites and Pause other players move to Preferences.
- **Legacy Scenes:** they duplicate My rooms, and none are saved locally.
- **Ctrl+4/5/6:** they opened hidden pages.
- **Classic window and `--ui=classic`** (removed in O5): every contract capability has
  a next-window path. The tag `listening-room-pre-classic-removal` marks the commit
  before the removal; reverting the removal commit restores classic.
- **Shuffle, Open category, per-player mute, Ctrl+W and the standalone vista window:**
  not part of the personal-radio contract. Rooms carry scenes, and Other players keeps
  per-player volume.
- **Video with sound for a bare video-file link:** the Link sheet never offered it. A
  YouTube link plays its audio through Play, while a plain video file opens only as a
  silent scene. This is a recorded loss. *Reversed for YouTube on 2026-11-20* (see
  Decisions taken): YouTube videos can now be watched with sound. A plain video-file link
  still opens only as a silent scene.

## Acceptance criteria

The acceptance author owns these checks and the probe oracles, separately from the
implementer. A criterion that evidence shows to be wrong is revised in place with the
reason; it is never silently weakened.

1. **Time to sound.** With a last room recorded and nothing playing, sound starts after
   one activation from launch (Return). A catalogue room takes two activations (card,
   then Enter), or one with double-click or Enter.
2. **No autoplay.** Launching, selecting, previewing, filtering, searching, and pasting
   or dropping a link send no start command to the controller. The probe asserts this
   against the fake backend.
3. **Now at rest** shows only: back, Output, Timer, Menu, the room name, the now-playing
   line, the source chip, Mix, play/pause, room volume, and skip / More / Less when the
   source supports them.
4. **Vocabulary.** "Main audio", "Background audio", "Retry picture", "Refresh" and
   "Picture" do not appear in Rooms, Listen or Now. "Workspace 9" appears only as a
   placement value.
5. **Capability controls.** Controls the source can never use are hidden, not disabled.
6. **Layer failure.** A forced Ambience start failure keeps Music and Scene playing and
   the previous Ambience available. It shows one banner with Try again and Choose
   another, and a marker on Mix. Try again re-runs only that layer.
7. **Auto-save.** Station mood changes persist across restart without a Save button.
   Each change produces one toast with Undo.
8. **Room volume** scales Music and Ambience and keeps their ratio within one step.
   Mutes persist. MPRIS, roomctl and Waybar report the same value.
9. **Keyboard and accessibility.** Every probe flow completes with the keyboard only.
   Icon-only buttons have accessible names. Focus is visible over art.
10. **Contrast.** Text over art reaches 4.5:1 against the 90th-percentile luminance of
    the pixels behind it, in all eight rooms. The probe samples this from screenshots.
11. **Adaptive.** At 1200, 800 and 420 px widths there is no horizontal scroll and no
    clipped control. Below 560 px the mini layout shows.
12. **Reduced motion.** No moving scene in the app or in the Scene window, no idle fade
    and no visual crossfade. Audio fades remain.
13. **Parity.** Every capability in the personal-radio contract is reachable through the
    mapping table above. The probe walks each path, and nothing becomes CLI-only.
14. **Evidence.** `tests/probes/listening-session-ui.py` captures: first run, return
    available, Rooms, preview, Now (radio), Now (library with skip), Mix, layer failure,
    timer running, Listen search, station detail, link sheet, Connections, mini player,
    and reduced motion.

## Decisions taken

- **Adopt libadwaita.** The package gains the libadwaita typelib. It renders the same
  outside GNOME, and the dark scheme is forced.
- **Animated scene in Now.** Play the reviewed room loop, muted, while the window is
  visible and motion is allowed; pause it when the window is hidden. Decode cost is measured on a real desktop. If the cost is noticeable, ship still art instead.
- **Room volume is a controller concept,** not a UI-only scale, so every surface agrees.
- **Retire Scenes and settings profiles.** No saved user data is affected (checked
  2026-11-17).
- **YouTube: both a silent scene and with sound (user decision, 2026-11-20).** The
  user's words: "I'd offer both options for silent scene and sound". This reverses the
  recorded loss under Retired for YouTube. Every YouTube row and YouTube link offers
  Play (audio as the room's Music), **Watch** (video with sound) and **Show as scene**
  (silent, the picture layer only). Watch is its own player window (the existing `video`
  mode of the picture engine, under its own app id), not a layer, and never audio and
  picture as two players: two independently resolved mpv processes have no shared clock,
  so they would start seconds apart and drift. Starting Watch pauses the room's Music and
  Ambience the way Pause does, with the resume offer recorded; the Scene keeps going. Now
  shows "Watching · ‹title›" with Stop watching. When the video stops (Stop watching, the
  window closed or the video ended) a toast offers Resume; it never resumes on its own.
  With no room, Watch just plays. Show as scene marks the room Changed, with Save as my
  room. The controller gains an additive `video` action (start, stop) and a `video`
  snapshot field.
- **YouTube suggestions before you type, without an account.** The YouTube scope shows
  Your saved YouTube (stations and history, no network), "For ‹room›" (short searches
  built from the room's own tags and subtitle) and "Live now" (YouTube's live filter).
  The last results are cached on disk, shown at once, refreshed off the GTK thread at
  most hourly and dropped after a day; a failed refresh keeps what was there.
  Personalised recommendations need the user's YouTube account and are not built.
