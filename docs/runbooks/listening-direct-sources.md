# Listening Hub direct sources

Checked 2026-11-14 with public HTTP requests and bounded reads, without audio playback. Live endpoints and regional rights can change; the station website remains a separate, explicit action if playback fails. A failed stream never opens the browser automatically.

| Catalogue ID | Source and evidence | Check |
| --- | --- | --- |
| `ylex` | [YleX on Yle Areena](https://areena.yle.fi/audio/ohjelmat/ylex); Yle-owned `https://icecast.live.yle.fi/radio/YleX/icecast.audio` | HTTP 200, `audio/aacp`, first 1,024 bytes received. |
| `yle-klassinen` | [Yle Klassinen](https://yle.fi/aihe/a/20-34814); Yle-owned `https://icecast.live.yle.fi/radio/YleKlassinen/icecast.audio` | HTTP 200, `audio/aacp`, first 1,024 bytes received. |
| `radio-dismuke` | [Station's listening page](https://early1900s.org/radiodismuke/) links its [media player playlist](https://early1900s.org/radiodismuke/radiodismuke.pls). | Playlist HTTP 200; both listed streams returned HTTP 200 `audio/mpeg`, first 1,024 bytes received. The playlist is HTTPS but its stream URLs use unencrypted HTTP. |

Previously published Yle HLS addresses for these two stations returned HTTP 400 in this environment. The successful Yle Icecast addresses were discovered via station listings and belong to Yle; a bounded read confirms media response, not continuous playback or programme identity. The station pages provide context but do not themselves publish those Icecast addresses. Geographic access and service availability may vary.

YouTube Music search entries remain browser discovery. An explicitly supplied public video URL can be sent to `start_youtube_audio`: it accepts a single YouTube video, strips playlist and other parameters, and uses mpv with yt-dlp configuration ignored and playlist expansion disabled. It does not read browser cookies or sign into an account. Availability, region and platform restrictions may prevent playback; authenticated libraries remain in their own clients.
