# tango-yt-dlp-plugin

A yt-dlp extractor for live streams on [tango.me](https://www.tango.me). It
records with ffmpeg, works anonymously, and keeps running past the site's
ten-second CDN token by refreshing it through a small local HLS relay.

## Requirements

- yt-dlp 2024 or newer (`yt-dlp --version`)
- ffmpeg on `PATH` (yt-dlp hands live HLS to ffmpeg)

## Install

```sh
./install.sh
```

The script symlinks `yt_dlp_plugins/` into `~/.config/yt-dlp/plugins/tango-yt-dlp/`
and checks that yt-dlp picked it up. Edits in this checkout take effect on the
next run.

Without installing, point yt-dlp at the **parent** folder of this repo:

```sh
yt-dlp --plugin-dirs ~/Projects -f 1 "https://www.tango.me/<alias>"
```

`--plugin-dirs` takes a container folder, not this project folder: yt-dlp
scans its children for `yt_dlp_plugins/`.

## Use

Every form resolves to the same stream:

```sh
yt-dlp -f 1 "https://www.tango.me/stream/jBLKPPFpEsXV2S2C7nC39A"
yt-dlp -f 1 "https://www.tango.me/live/jBLKPPFpEsXV2S2C7nC39A"
yt-dlp -f 1 "https://www.tango.me/wisdomhood"
yt-dlp -f 1 "https://www.tango.me/profile/wisdomhood"
yt-dlp -f 1 "https://www.tango.me/@wisdomhood"
```

`tango.me/<alias>` and the shared `tango.me/@<alias>` form are looked up
through the profile API, so they work for aliases you have never opened in a
browser.

List who is live right now:

```sh
yt-dlp --flat-playlist "https://www.tango.me/live"
```

Wait for an offline profile to go live:

```sh
yt-dlp --wait-for-video 60 -f 1 "https://www.tango.me/<alias>"
```

Stop a recording with Ctrl-C. yt-dlp finalises the file and MP4-fixes it.

A live recording is dated. The extractor derives `timestamp` from how long
the broadcast has been running, and yt-dlp turns it into `upload_date`, so
`%(upload_date)s` in an output template is the day the stream began. The
value can be off by a few seconds.

### Anonymous vs logged in

| | Anonymous | `--cookies-from-browser chrome` |
| --- | --- | --- |
| Public streams | yes | yes |
| Followers-only / private | no | yes, if your account follows |
| Formats | 640x360, 1280x720 | same |
| Extra setup | none | a logged-in Chrome profile |
| Check-in per run | one visitor check-in | one visitor check-in on top of your account cookies |

No account is needed for normal use. Anonymous viewers get the same renditions
as logged-in ones; the difference is only which streams are visible.

### Formats

```
ID  EXT  RESOLUTION
0   mp4  640x360
1   mp4  1280x720
```

Use `-f 1` for the 720p rendition. Two things are worth knowing:

- The manifest claims `RESOLUTION=1280x720`, but broadcasts are often
  portrait. The finished file can be `720x1280`; that is what the site sends.
- The manifest has no `CODECS` attribute, so `-F` shows `unknown` for
  vcodec/acodec. The actual stream is H.264 + AAC.

### Logged-in and private streams

Anonymous access covers public streams. For followers-only or private streams,
reuse your browser session:

```sh
yt-dlp -f 1 --cookies-from-browser chrome "https://www.tango.me/<alias>"
```

The extractor still performs the site's visitor check-in, because the CDN token
endpoint only issues tokens to a visitor identity. Account cookies are left in
place for the stream lookup, so private access is preserved.

## How it works

The web player talks to `https://www.tango.me/api` (not the old
`gateway.tango.me`):

| Step | Request | Why |
| --- | --- | --- |
| 1 | `POST /visitor-lobby/api/public/visitors/v1/check-in?appId=tango` | sets `Tango-VT` (visitor JWT), `Tango-SD`, `Tango-DeviceId` |
| 2 | `POST /visitors/stream/client/v2/stream/{id}/watch?requestId=` | registers the viewer and returns the stream document (status, HLS URLs, anchor) |
| 3 | `GET /stream/token-data/v1/web/visitor` | returns `{token, username, expireAt}` with a 10-second TTL |

The CDN (`cinema-*.tango.me`) guards **playlists** with the cookie triplet
`tt` / `ttu` / `tte`; media segments are public. After ten seconds every
playlist request returns 401, which is why a naive download dies a few seconds
in.

yt-dlp always routes live HLS to ffmpeg, and ffmpeg cannot refresh cookies.
So the extractor starts `TangoHlsRelay` on `127.0.0.1` and gives ffmpeg that
URL. On every playlist reload the relay asks for a fresh token, fetches the
playlist, and rewrites segment URIs to absolute CDN URLs so the video itself
does not pass through Python. Master playlists keep pointing at the relay,
because variant playlists need the token too.

A proxy would swallow that loopback URL: yt-dlp passes `--proxy` (and
`http_proxy` from the environment) on to ffmpeg, which applies it to every
request it makes. The extractor appends `127.0.0.1` and `localhost` to
`no_proxy`/`NO_PROXY` when it runs, so the relay is reached directly while
the API and CDN traffic keeps going through the proxy.

If the token endpoint ever refuses, the relay replays the last playlist it
fetched (up to two minutes old) instead of failing, which costs a few
duplicated segments but keeps the recording alive. A refusal is also retried
with a fresh visitor identity. If the token stays unavailable beyond that
window, the relay appends `#EXT-X-ENDLIST` so ffmpeg finalises a playable file
rather than the recording failing.

## Limits

- **No DVR.** Live playlists hold an eight-second sliding window, so
  `--live-from-start` cannot work. Recording starts at the live edge.
- **Token refresh is cheap.** One token per ~8 seconds, about 450 per hour.
  Measured on a single visitor identity: 400 token calls in 188 s and 200
  check-in-plus-token rounds in 191 s, none refused. Long recordings are
  comfortably inside the budget.
- **Viewer count.** Calling `watch` adds one viewer, so the reported
  `concurrent_view_count` includes this download.
- **Ended streams.** Tango drops a stream from every route once it ends: the
  profile stream lookup 404s and `watch` answers `FAILED_TERMINATED`, with no
  HLS URL left anywhere. A download has to start while the stream is live.
  If a stream ends *during* a recording, the CDN starts answering 404 or 410
  for playlists the relay had already served; the relay marks the last one
  with `#EXT-X-ENDLIST`, so ffmpeg finalises a playable file instead of the
  download failing.

## Troubleshooting

- `Unsupported URL` — the plugin is not loaded. Check
  `yt-dlp --verbose … | grep 'Extractor Plugins'`.
- `ffmpeg not found` — install ffmpeg; live HLS cannot go through yt-dlp's
  native downloader.
- `Could not refresh stream token … 403` — the token endpoint refused. The
  extractor retries with a fresh visitor identity and keeps replaying the last
  playlist for two minutes before ending the file cleanly.
- `Stream unavailable (FAILED_TERMINATED)` — the stream has ended; only live
  streams carry an HLS URL.
- Recording stalls behind a proxy — the plugin already exempts the loopback
  relay from it, so a stall means the proxy is failing the CDN traffic
  itself.

## Tests

The relay logic (playlist rewriting, throttle replay) and the visitor-session
rules are covered offline. Run the suite with the interpreter that ships with
yt-dlp:

```sh
PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python -m unittest discover -s tests
```

## Files

- `yt_dlp_plugins/extractor/tango.py` — the extractor and relay
- `tests/test_relay.py` — offline relay tests
- `tests/test_session.py` — visitor token / identity-reset tests
- `tests/test_urls.py` — URL routing tests
- `install.sh` — plugin-dir installer
- `tango_chat_recorder.py` — experimental chat-to-SRT tool (see its header)
