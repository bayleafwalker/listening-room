# Listening Room: a programme across devices

Proposed 2026-11-22; not yet accepted. Revised the same day so that the MCP server
is deployed rather than local (decision 5). Source: an external design review that
proposed "a listening room with a continuing programme, optional audio layers, and
a presence across devices", assessed against 2.5.0 (`70db15e`). This plan keeps the
review's direction and narrows its first scope to one listener on several devices.
When accepted, it amends one clause of [personal radio](listening-room-modern-radio.md):
"No desktop composed-mix transfer is implied" stays true for Music Assistant, but
station streams (below) add a composed mix that leaves the desktop.

## Where 2.5.0 stands

- **Rooms and stations are already separate.** A room holds atmosphere, scene, levels
  and placement. A station holds continuous programming from approved sources, with a
  short prepared queue (`station_engine.py`, eight items) that refills ahead. The
  review's *room template* and *station* match these. Its *live room* and *listener
  session* do not exist yet: there is one session per user, over a per-user Unix
  socket that checks the peer's credentials.
- **The mix is already personal.** Music, Ambience and the optional layer are separate
  mpv players with their own level and mute. That is the review's "capable client".
- **Capabilities are already stated** as browse, local playback, remote playback and
  external handoff. The review adds two the app does not model yet: mixable and
  redistributable.
- **Music Assistant is an optional adapter** (browse, play on a Music Assistant player,
  move a queue between players). It is not configured on the workstation, and no
  deployment of it was found in gitops-nixos, datacluster or appservice.
- **The library lives on appservice.** Jellyfin and AudioMuse run in its `media`
  namespace. The internet reaches that cluster only through cloudflared, and people
  reach it over WireGuard.
- **The phone is a remote**, through KDE Connect commands. It does not play anything.
- **Nothing is reachable off the machine.** The session socket is local, and the
  stream relay binds to 127.0.0.1.
- **There is no speech layer and no MCP server.** The workstation runs a local LLM
  endpoint (llama-swap, loopback only). No local text-to-speech model is configured.
- **YouTube and Twitch play locally**, through yt-dlp and native channel playback.

## Decisions

1. **One listener on several devices comes first.** Rooms with several listeners and
   independent control wait until a second listener actually exists. Station streams
   give two people the same programme, each with presenter or music only, without a
   room server.
2. **Music Assistant stays an adapter, not the engine.** Its documentation (read
   2026-11-22) says AI Radio is alpha, puts one show on air at a time, and clears the
   target queue when a show starts. The FastMCP plugin is experimental, Sendspin is a
   technical preview, and the desktop companion is early alpha. The mobile app is not
   meant for offline playback, and its phone player cannot be found or controlled by
   other apps by default. Clearing the queue and running one show at a time conflict
   with this app's layer preservation and with having several rooms. Two designs are
   worth borrowing: AI DJ filling the gaps of an existing queue, and FastMCP's
   per-client tokens with read-only defaults. A runtime test can reopen this decision.
3. **Speech is its own layer with a budget.** It has a presenter role (between tracks),
   an atmosphere role (incidental, repetition-limited) and a primary role (podcast,
   live speech, talk radio). There is one foreground speaker at a time.
4. **Sources declare whether they may leave the machine.** Only sources allowed to
   leave reach a stream that another device or person hears. Library files and
   licensed offline sounds are allowed on the owner's devices. YouTube and Twitch stay
   local layers. Whether a source can technically be played and whether it may be
   used are separate checks.
5. **The MCP server is deployed on appservice, beside the library.**
   - Read and compose run there, against the library and saved stations. Perform on
     a station stream (P2) also happens in the cluster.
   - Perform on the desktop session goes over a connection that the desktop session
     opens to the service. Nothing new listens on the workstation, and a workstation
     that is off shows as unavailable.
   - The service checks the read, compose, perform and configure grants on every
     request.
   - vuoro-cloud is the design to copy, not the host. Its gateway already serves
     claude.ai and ChatGPT over OAuth 2.1 with pre-registered clients. It re-checks
     the grant on every request and maps each tool to a scope, denying any tool
     without one (vuoro-cloud D-046). But its `/mcp` only proxies the Vuoro runtime,
     and hosting other apps is a listed non-goal. Its scope also rules out exposing
     the operator's home network, which fronting a home service would need.

## Acceptance

### P1: a presenter in the desktop app

- Existing stations and rooms stay silent between tracks. A station with the presenter
  on follows a budget: segments every few tracks at most, each with a maximum length.
  The budget is visible and adjustable. "Less talking" lowers it, and Music only turns
  the presenter off. Both are feedback on the station and do not change the station.
- A segment is ready before its slot, or it is dropped. A dropped segment leaves no
  gap and no error card. With the LLM endpoint or text-to-speech unavailable, the
  station plays music only and the status says so quietly.
- Segments play on a speech layer with its own level and mute. Muting it never
  advances or delays the music.
- Factual statements come only from captured metadata (library tags and the metadata
  pipeline's provenance), and each segment stores its sources. An item with no
  metadata gets no factual segment. A segment does not repeat within a station's
  history window.
- A primary speech source (podcast, live channel, a station marked speech) suppresses
  presenter segments while it plays.
- Prerecorded clips go through the same slot scheduler, with source, allowed placement
  and expiry.
- Checks use fake LLM and text-to-speech endpoints on loopback (netguard). They force
  a timeout, malformed output and a missing model, and they include a GUI probe of
  the speech layer.

### P2: station streams

- An explicit action starts a station headless on appservice, beside Jellyfin. It
  publishes a mixed stream in two variants, full and music only, to the owner's
  authenticated devices over the LAN and WireGuard. Station streams never get a
  public route, and no listener without authentication is accepted on any interface.
- A listener joins at the current position. Pausing stops only that listener's
  output, and resuming rejoins live. In music only, the presenter's slots are filled
  with music bed or ambience, so the programme clock does not move.
- Sources not allowed to leave are skipped, and the reason is visible.
- On the desktop, "Continue on phone" gives a link or QR code. Starting sound on a
  device remotely is not assumed.
- Two clients on different variants stay on the same programme. Measure their offset
  and state it. The UI says "same programme", not "in sync".

### P3: the car and other phones

- The P2 stream plays on Android and through Android Auto in an existing player app,
  surviving screen lock and a change between Wi-Fi and mobile data. Navigation prompts
  and calls take priority, and afterwards the stream rejoins live.
- Offline: a programme of a chosen length is pre-rendered from library sources and
  downloads to the phone as a file.
- Jolla: the same stream plays on the device in the background (a stock or thin
  native player). This makes no Android Auto claim.

### P4: MCP

- **On the LAN first.** The service runs on appservice and is reachable over the LAN
  and WireGuard, with a token for each client. Claude Code and Claude Desktop connect
  to it.
  - Read and compose create stations and rooms and return a Play here link without
    starting sound.
  - Perform acts only on the granted session or station stream.
  - Tests show that a session without a grant, and other rooms, stay untouched. A
    desktop session that is off or has not connected reports unavailable.
- **Then a public route for claude.ai and ChatGPT.** A first-level `kotona.app` name
  through cloudflared, with an OAuth 2.1 authorization server and pre-registered
  clients.
  - It is a protected appservice change: the public exposure allowlist, the
    cloudflared egress policy and the identity class.
  - Revoking a grant takes effect on the next request.
  - Authentik is the first choice for the authorization server. Its current OAuth
    provider serves a ChatGPT GPT Action, so serving MCP clients is unproven. If it
    cannot, the service carries its own authorization server, modelled on
    vuoro-cloud's.
- History marks each play as chosen, played by a station, or chosen by someone else.
  Inferred preferences can be inspected and corrected. "From what I chose" uses
  chosen plays only.

## Deferred, with reasons

- **Live rooms with several listeners**, per-listener rendering and synchronised
  playback across devices: wait for a second listener (decision 1).
- **Twitch speech in a mix, and rebroadcasting a room:** the documented embed gives no
  reusable speech feed (decision 4).
- **Fronting the MCP server through vuoro-cloud:** needs a change to vuoro-cloud's
  scope first (decision 5).
- **Listening Post as a room type:** the review cites an earlier design that is not in
  this repository. It needs its source document first.

## Open questions

- **Text-to-speech engine for P1.** It must run locally and not block preparation.
  The model choice belongs to the implementer, within that constraint.
