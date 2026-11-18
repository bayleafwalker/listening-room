# Twitch shuffle and the audio layer

Listening Hub has Music and ASMR collections. Add Twitch channel **logins** to each collection in the app; exclusions apply to both styles. The preferences file is `$XDG_CONFIG_HOME/listening-hub/twitch.json` (usually `~/.config/listening-hub/twitch.json`). It contains only names and exclusions, not credentials. No channels are preselected and nothing starts automatically. Shuffle excludes the preceding channel. If all choices are excluded or only the preceding channel is available, there is no eligible result.

Without configured Twitch API access, curated channels can still be selected, but their live status is **unknown**. When configured, discovery resolves the Music or ASMR category through [Twitch Get Games](https://dev.twitch.tv/docs/api/reference/#get-games) and requests currently live category and curated streams through [Get Streams](https://dev.twitch.tv/docs/api/reference/#get-streams). This is a bounded first page, not a complete inventory or an endorsement of every result. A curated channel is eligible only while it is live in the selected category; one streaming another category is reported as a style mismatch. An empty successful query is shown as no live channels in the style; an API or network failure leaves status unknown. A channel can go offline after discovery.

To enable discovery, arrange a Twitch application and obtain an app or user access token using Twitch's [authentication documentation](https://dev.twitch.tv/docs/authentication/). A complete `TWITCH_CLIENT_ID` and `TWITCH_ACCESS_TOKEN` environment pair takes precedence. Otherwise the app reads the existing Twitch CLI login at `$XDG_CONFIG_HOME/twitch-cli/.twitch-cli.env` (normally `~/.config/twitch-cli/.twitch-cli.env`), then the legacy `~/.twitch-cli/.twitch-cli.env` location. That file must be a regular, non-symlink file owned by the current user with no group/other permissions (`chmod 600`). Normal discovery uses `CLIENTID` and `ACCESSTOKEN`. If Twitch rejects a saved access token with HTTP 401, the app may use that same private CLI file’s `CLIENTSECRET` and `REFRESHTOKEN` to renew the existing grant. It retries discovery or follows import once and atomically rotates both tokens in the original file, preserving other settings and private permissions. Credentials are never copied into room presets/preferences or displayed. Concurrent app renewals share a private file lock; observed CLI replacements are preserved. The file is reread on each discovery/import, so renewing through the CLI takes effect without restarting the room. Both credentials must correspond to the same Twitch application. Partial environment overrides are rejected rather than mixed with saved credentials. Explicit environment credentials remain read-only and are never renewed by the app. A missing or rejected refresh token requires reconnection through the CLI. Authentication, permission, rate-limit and network failures have distinct messages and never label uncertain channels offline. Never put credentials in this repository. A shell `export` alone does not persist credentials or update a previously running GUI.

The optional overlay is a separate audio-only mpv user service with its own socket, playback controls, and initial volume of 20%. Its volume and mute settings are remembered when changing or stopping channels, including adjustments made while stopped. It does not change system master volume or operate the radio, browser, video, or ambience service. Starting a replacement stops only the previous overlay. Stop the overlay explicitly when finished. Playback uses installed mpv and yt-dlp on a public channel URL and depends on their current Twitch support and network availability. Playback can fail despite a live result, including for access restrictions; use the channel's normal Twitch page in a browser when necessary. The layer reports a load failure or timeout and does not claim success until mpv reports playback started. It does not acquire browser cookies or bypass authentication or ads.

Twitch's [official embedded player](https://dev.twitch.tv/docs/embed/video-and-clips/) is a web player with embedding requirements, including a parent domain and minimum dimensions. The audio layer is a local playback prototype, not a promise of an official frameless Twitch player or ad masking.

## Importing follows

The optional import reads channels followed by the profile you enter for review; it does not infer their style or automatically add them to a collection. It requires a Twitch **user** access token for that profile with `user:read:follows`, and the corresponding client ID. Twitch's [token validation endpoint](https://dev.twitch.tv/docs/authentication/validate-tokens/) checks the token's client, profile and scope before [Get Followed Channels](https://dev.twitch.tv/docs/api/reference/channels/#get-followed-channels) is called. An app token cannot access follows. The import reads up to five pages of 100; a partial result is marked incomplete. Review and explicitly choose channels for the desired collection. Missing, invalid or insufficient credentials cannot establish the follow list; they are not evidence that it is empty. No Twitch application registration or OAuth authorization is performed by the import.


## Workstation verification

The 2026-11-14 integration check reached actual playback for the public
`lofigirl` channel through the Atmosphere adapter, using a private temporary
unit, the packaged resolver and null audio. This proves that channel and path
at check time; it does not establish access to every channel, category
discovery, authenticated follows or continuous reliability. The real local
transport probe separately verified main/overlay replacement, retained volume
and mute, cancellation before start, and unchanged system volume. No Twitch
API credentials were set in the checking process, so Helix and follows remain
an outstanding integration gate.


## Browsing without extra controls

Channels uses a readable list with channel names, stream titles and compact
live/saved/excluded indicators. The filter searches the current result list by
name, login or title and highlights literal matches with escaped markup. All
and Saved are the only list filters; the count shows how many rows match.
Selecting, filtering, switching styles and Shuffle only preview a choice.
Play selected is the only playback action. Shuffle respects the visible list
and global exclusions. Ctrl+F focuses this tab's filter. Each style retains its results in memory until the
window closes. Saved membership follows the current pool, including removals.
Management, exclusions, follows review and browser links stay under a collapsed
Manage channels and more section. Importing follows never classifies them
without review. Search filters the bounded discovered list; it does not search
Twitch's entire catalogue.

On 2026-11-14 the existing CLI login was measured as containing a client ID,
client secret, access token and refresh token. Its file mode was changed from
0644 to 0600 without displaying any values. A metadata-only integration check
validated the expected profile and follows scope and successfully discovered
both styles and retrieved follows through the saved login. This establishes
current saved-login integration, not automatic token refresh or a complete
consumer OAuth flow.


On 2026-11-15 the saved CLI access token returned HTTP 401 from Twitch's
validation endpoint. The existing refresh grant successfully renewed it in
place; validation then returned 200 for the expected profile/client, and live
Music and ASMR discovery both succeeded. Version 2.0.1 adds the same bounded
reactive renewal path and distinct authentication, permission, rate-limit and
connection messages. This check did not start playback or import follows.
