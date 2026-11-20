# tango.me live streams: technical knowledge base

Last verified 2026-11-20 against the live site and yt-dlp 2026.10.26.
This note is the durable record: the contract, the measured behaviour, the
traps, and how to check each claim again. `PLAN.md` is the shorter design
record; `README.md` is for users.

## What this covers

`yt-dlp` records tango.me live streams through a plugin. It accepts a stream
id, a profile alias, `@alias`, `/profile/<alias>`, and the `/live` feed.
It works anonymously, or with `--cookies-from-browser chrome` for
followers-only streams. Output is H.264 + AAC in MP4; the video bytes never
pass through Python.

## The two facts everything follows from

1. **The CDN playlist token lives ten seconds.** `token-data` returns
   `{token, username, expireAt}` with `ttlSeconds: 10`, and the CDN wants all
   three as a cookie triplet `tt` / `ttu` / `tte`, with `tte` equal to
   `expireAt` exactly. Only playlists are guarded; media segments are public.
2. **yt-dlp never downloads live HLS in Python.** It unconditionally picks
   `FFmpegFD` for live HLS, and ffmpeg cannot refresh cookies. Forcing
   `protocol = m3u8_native` does not help either: `HlsFD` is single-pass and
   does not loop.

Together they mean a recording dies at the first token expiry (about eleven
seconds) unless something between ffmpeg and the CDN refreshes the token. The
relay is that something.

## API contract

Base: `https://www.tango.me/api`. The old `gateway.tango.me` API is retired;
anything targeting it is dead code.

| Step | Request | Contract |
| --- | --- | --- |
| Check-in | `POST /visitor-lobby/api/public/visitors/v1/check-in?appId=tango`, body `{}` | Sets `Tango-VT` (visitor JWT, ~30 days), `Tango-SD`, `Tango-DeviceId` |
| Watch | `POST /visitors/stream/client/v2/stream/{id}/watch?requestId=` | Mandatory viewer registration; returns `details.stream`, `settings`, `viewInfo.hlsStreamInfo`, `status`, `anchor` |
| Token | `GET /stream/token-data/v1/web/visitor` | `{token, username, expireAt}`, ten-second TTL |
| Alias | `GET /visitors/profilealias/api/alias/convert?alias=X` | 22-char account id as plain text; empty body with HTTP 200 when absent |
| Stream id | `GET /showroom-streams/api/public/stream/v1/id?broadcasterId=<account>` | stream id, or 404 when offline |
| Feed | `POST /showroom-streams/api/public/web/catalog/v1?traceId=<uuid>` | `catalogItems[].encryptedStreamId` |

`GET /stream/token-data/...` requires `Referer: https://www.tango.me/`,
otherwise it answers `400 Referer header is empty`. `Origin` is sent too.
The `foreground-id`, `interaction-id` and `x-app-client-session-id` UUID
headers are optional camouflage; requests work without them.

`watch` must precede every playlist fetch. A valid token without a prior
`watch` still gets 401, because `watch` is what authorises the visitor for
that stream.

### The HLS manifest

The watch response's `masterListUrl` is a master playlist with two
renditions. The manifest claims `1280x720` and `640x360`, but broadcasts are
often portrait and the file comes out `720x1280`; the manifest is not
corrected for rotation. It carries no `CODECS`, so `-F` prints `unknown` for
vcodec and acodec. The actual codecs are H.264 and AAC.

Media playlists use `EXT-X-TARGETDURATION:1` and an eight-segment sliding
window with no DVR. There is no earlier data to fetch, so `--live-from-start`
cannot work. A recording starts at the live edge.

### Failure codes, not HTTP statuses

`watch` answers HTTP 200 with a semantic code:

| Code | Meaning | Usual cause |
| --- | --- | --- |
| `FAILED_WRONG_STREAM_ID` | No such stream | Typo or stale id |
| `FAILED_TERMINATED` | Broadcast over | Stream ended before the download started |
| `FAILED_BAD_REQUEST` | Lookup refused | Audio rooms are the known case |

A wrong id is never a retry. Treat 200-with-code as the answer.

## Session rules

The extractor checks whether an unexpired `Tango-VT` cookie exists, and
checks in when it does not.

A logged-in Chrome profile is easy to misread: it carries
`.www.tango.me Tango-SD` with `sessionType: DEFAULT` **and** a non-null
`accountId`, plus `Tango-ST`, `Tango-RT`, `Tango-WST`, and no `Tango-VT`.
So "logged in" is `accountId` set, never `sessionType == 'ACCOUNT'`, and a
logged-in run still performs one visitor check-in to obtain a CDN token. The
account cookies stay in the jar for the stream lookup, which is what makes
followers-only streams visible.

Check-in cookies are host-only for `www.tango.me`. If a fake or a test reply
sets `Domain=www.tango.me`, the standard library stores them as
`.www.tango.me` cookies instead, which changes which requests send them.

## Architecture

`TangoHlsRelay` is an HTTP server on `127.0.0.1` that fronts
`cinema-*.tango.me` for ffmpeg.

- Each playlist request fetches upstream with the current triplet in a plain
  `Cookie` request header: `tt=...; ttu=...; tte=...`.
- The triplet is never written to yt-dlp's cookie jar. The jar would apply it
  to unrelated requests, and ffmpeg cannot read a jar anyway.
- Master playlists are served rewritten so their variant URLs point back at
  the relay, because variants need the token too.
- Media playlist segment lines are rewritten to absolute CDN URLs. Segments
  are public, so the video path skips Python and the relay sees roughly one
  request per second.
- The relay refreshes the token itself. `MARGIN = 2` seconds means a refresh
  about every eight seconds, not every six: a third fewer calls to a
  rate-limited endpoint.

Earlier approaches and why they were dropped:

- **Cookie-jar monkeypatch.** Hooking `cookiejar.add_cookie_header` to
  refresh before any request works, but it leaks across extractors in a mixed
  playlist and refreshes without knowing which stream is downloading.
- **Query-param auth.** `?tt=` and `?token=` do not authenticate. The
  triplet must be cookies (or a `Cookie` header).
- **Raising the TTL.** `ttlSeconds` is fixed at ten; query parameters do not
  change it.

### Proxy interaction

ffmpeg takes the proxy from the environment (`http_proxy`, which yt-dlp sets
from `--proxy`) and applies it to every request, including the loopback relay,
so a proxied run stalls. The extractor appends `127.0.0.1` and `localhost` to
both `no_proxy` and `NO_PROXY` (extend, never replace; trimmed and
deduplicated; idempotent). It writes both spellings because ffmpeg reads the
lowercase one and some other tools read the uppercase one. API and CDN traffic
still uses the proxy.

`no_proxy` alone is not enough for yt-dlp's **own** requests. Under `--proxy`,
yt-dlp sets `proxies = {'all': proxy}` with no `'no'` entry, and
`select_proxy` consults `no_proxy` only when `'no'` is present
(`yt_dlp/utils/networking.py`). The extractor's one Python-side fetch of the
relay's master playlist would therefore be tunnelled through the proxy. A
proxy on another host resolves `127.0.0.1` to itself, answers 502, and the run
dies with "HLS manifest could not be read" — which reads like an auth problem.
The fix is per-request: fetch the master with
`Request(url, proxies={'all': '__noproxy__'})`, which yt-dlp turns into "no
proxy" for that request. The relay's own CDN fetches keep using the proxy.

This only shows up against a proxy that cannot reach loopback. A proxy running
on the same machine answers `127.0.0.1` happily and hides the bug; the live
check below therefore used one that refuses loopback.

## Degradation policy

The relay has three distinct end states. They are not interchangeable.

| Situation | Behaviour |
| --- | --- |
| Token refresh refused (403) | Retry, then reset visitor identity (check-in again), keep replaying the last playlist for up to 120 s |
| Token still unavailable after 120 s | Serve the cached playlist with `#EXT-X-ENDLIST`; ffmpeg finalises a playable file |
| CDN answers 404/410 for a playlist already served | End the recording immediately: cached body (or a minimal ended playlist) plus `#EXT-X-ENDLIST`, warn once, answer later requests from cache |
| CDN answers 404/410 for a playlist never served | Raise; a bad URL should stay loud |
| 5xx control call after retries | `ExtractorError(expected=False)`; the site is at fault, not the URL |
| 401/403 control call after retries | `ExtractorError(expected=True)` naming `--cookies-from-browser chrome` |

Two details matter:

- The 404/410 check runs before the retry decision, so an ended stream does
  not burn a token refresh.
- An already-ended cached body is returned at the top of `fetch`, before any
  upstream request. A test that seeds an ended cache therefore proves the
  short-circuit, not the throttle-replay branch. This was a real trap: the
  replay test passed while exercising the wrong path until the final review.

The `FAILED_TERMINATED` mapping is only reachable when the stream ended
before the download started. Once a stream ends, tango removes it from every
route: `watch` answers `FAILED_TERMINATED` with no details, the profile
lookup 404s, and the page has no HLS URL. A finished stream cannot be
downloaded.

## Measured limits

- Token budget: 400 token calls at 4/s (188 s) and 200 check-in + token
  rounds at ~1/s (191 s), each on one fresh visitor identity, none refused.
  A one-hour recording needs about 450 tokens, far inside that.
- Recordings verified live: 239 s, 72 s, 34 s, 38 s, 45 s. All clean on
  SIGINT, H.264 + AAC, 720x1280. The 72 s run used
  `--cookies-from-browser chrome` by alias; that was the path that used to
  fail about one run in three.
- The 403 during development was self-inflicted: about 36 short-lived
  processes each imported the same logged-in Chrome jar, so one account
  identity checked in dozens of times a minute. A fresh visitor from the same
  address got 200 at the same moment, so refusal is keyed on identity, not
  address or token frequency. `datadome`, `_dd_s`, analytics cookies and
  file-vs-browser jars were ruled out by A/B tests.
- `watch` increments the viewer count, so the reported viewer number
  includes this download.

## Live verification (2026-11-20, merged build)

Every run below used the installed plugin and a real broadcast; durations are
`ffprobe`'s, not the wall clock.

| Run | Result |
| --- | --- |
| Stream id, 125 s cap | 115.6 s, 720x1280, H.264 + AAC, clean on SIGINT, no warnings. 1 check-in, 1 `watch`, ~14 token refreshes |
| `@jazz69`, `/profile/jazz69` | alias resolved, stream lookup ran, `The channel is not currently live` — the alias path is proven end to end |
| `--proxy` at a proxy that refuses loopback, 30 s cap | 29.4 s, 22 segments, zero loopback requests through the proxy, CDN traffic proxied |
| `--cookies-from-browser chrome`, 30 s cap | 3711 cookies read, one token 403, forced re-check-in, then 26.8 s of 360x640 video — the recovery path works live |

Two details from those runs:

- The feed's `broadcaster.alias` was null for all 30 live entries, so an alias
  cannot be discovered from the feed. Alias URLs still work; they are a legacy
  identity route, not the current one.
- A token 403 with a browser jar is expected once: the jar's identity is
  reused, and `_issue_token` resets the visitor and retries. The recording
  starts one or two seconds later; it is not a failure.
- The Chrome run picked the 360x640 rendition with `-f 1` because that master
  listed the low variant first. The manifest order is not quality order.

## Packaging and plugin loading

- yt-dlp loads plugins from the `yt_dlp_plugins` namespace package, from
  `sys.path` and its config directories. It does not scan entry points in
  2026.10.26.
- `[project.entry-points."yt-dlp.extractor"]` is invalid for newer
  setuptools because of the hyphen in the group name, and it is unnecessary.
  It was removed; the wheel only needs
  `yt_dlp_plugins/extractor/tango.py`.
- `yt-dlp --list-extractors` prints before plugins load
  (`yt_dlp/__init__.py:969` vs `:980`), so it can never list a plugin. Prove
  registration with a `YoutubeDL` instance and `gen_extractor_classes()`:
  expect `['tango', 'tango:live']`.
- `install.sh` symlinks `yt_dlp_plugins/` into
  `~/.config/yt-dlp/plugins/tango-yt-dlp/`. A bare
  `~/.config/yt-dlp/plugins/extractor/` directory is a stale layout yt-dlp
  does not load; it should not exist next to the symlink.
- `install.sh` pipes yt-dlp into `grep`. Without `-q`, grep reads all input;
  with `-q` it exits early, yt-dlp dies of SIGPIPE, and `pipefail` reports a
  false "plugin did not load".

## Test harness notes

The offline suite runs the real extractor against a fake transport handler,
`tests/fake_net.py`. It replaces `ydl._request_director` with
`ydl.build_request_director([FakeRH, UrllibRH])` and routes by
`(method, hostname, path)`. The loopback relay is not faked; it reaches the
fake CDN because the CDN host ends in `tango.me`.

Traps that cost time:

- `FakeRH` must accept only `*.tango.me`, so loopback requests fall through
  to `UrllibRH`. It also has to pop the `timeout`, `cookiejar` and
  `legacy_ssl` extensions, because this machine's `no_proxy` environment
  otherwise makes the handler decline every request.
- `RetryManager` re-raises the last network error with `expected=True`, even
  though the site is at fault. The extractor re-wraps the exhausted error as
  `ExtractorError(orig_msg, cause=..., video_id=...)` under the `except`
  handler, which makes `expected=False` and keeps the cause for the 401/403
  check.
- `expected_status=404` on `_download_webpage` yields the empty body, so
  "no such profile" and "not live" stay single-request semantic answers.
- `note=None` is not silence. For `None`, yt-dlp calls
  `report_download_webpage` and prints its default "Downloading webpage"
  line; only `note=False` suppresses it. The token refresher passed `None`
  and printed a line every eight seconds for a whole recording, and the test
  pinned `None` as if it were silence. Both are fixed; assert `False`, not
  `None`, when the point is quiet.
- `_parse_m3u8_formats_and_subtitles` does not fetch variant playlists for a
  master unless `hls_split_discontinuity` is on. That makes the master fetch
  the only Python-side request in a proxy-free relay path, which is why one
  per-request proxy bypass is enough.
- Fixtures: only `watch.json` came from a live capture. The rest are
  reconstructed from the contract in `PLAN.md`, with fixed scrubbed ids.
  `tests/fixtures/README.md` records which is which.
- A test that forces retries prints `RetryManager` warnings by design. Build
  the downloader with `params={'no_warnings': True}` where the warning is not
  the thing under test.

## Debugging playbook

Read the plugin list first; most "Unsupported URL" reports are an unloaded
plugin:

```sh
yt-dlp --verbose --simulate "https://www.tango.me/live" 2>&1 | grep 'Extractor Plugins'
```

Check the live feed and one stream:

```sh
yt-dlp --flat-playlist "https://www.tango.me/live"
yt-dlp -F "https://www.tango.me/<alias>"
```

Record with the token and relay visible:

```sh
yt-dlp -v -f 1 "https://www.tango.me/<alias>"
```

Expected in `-v`: one check-in, one `watch`, a token about every eight
seconds as a debug line (not a progress line), and relay requests to
`127.0.0.1`.

If a playlist 401s immediately, either `watch` did not run for that stream or
the `tte` value does not equal `expireAt`. If a playlist fetch stalls behind
a proxy, check that `no_proxy` reached the ffmpeg process.

## Open questions

- Followers-only and private streams are implemented but not proven end to
  end; that needs an account that follows a live broadcaster.
- Audio rooms answer `FAILED_BAD_REQUEST` to `watch`. Whether they expose HLS
  at all is undecoded.
- `tango_chat_recorder.py` still targets `wss://gateway.tango.me/ws`; the
  live chat WebSocket has not been decoded.
- The relay closes its server on extraction failures, but a long-lived
  process that abandons a recording without closing can hold the port for the
  900-second idle timeout. The server is daemon-threaded and self-reaping.

## Sources

- `PLAN.md` — decoded contract, measurements, design decisions.
- `docs/superpowers/specs/2026-11-20-tango-hardening-design.md` — the
  hardening round's intent and constraints.
- `tests/fixtures/README.md` — fixture provenance.
- `~/.ai-audit` ledger entries for 2026-11-19 and 2026-11-20 — session
  records, including the failures that produced these rules.
