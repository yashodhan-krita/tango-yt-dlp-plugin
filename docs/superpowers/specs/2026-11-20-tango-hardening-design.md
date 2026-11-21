# Tango.me extractor: offline harness and hardening

Date: 2026-11-20
Status: implemented (branch head `053b07b`, merged to `main` at `a0ba6e6`)
Supersedes nothing; extends `PLAN.md` and `README.md`.

## Intent

The extractor records tango.me live streams today: anonymous, with a logged-in
browser profile, by stream id, by profile alias, by `@alias`, and from the
`/live` feed. Recordings of 239 s, 72 s, 34 s, 38 s and 45 s finished cleanly on
SIGINT as H.264 + AAC 720x1280, with no warnings.

What is missing is confidence between live runs. Almost nothing that matters is
covered by a test: `_extract_stream` (the whole resolve → watch → token →
manifest path) has never run without the network, and the relay's failure
handling has only been exercised by hand.

This round adds an offline harness around the real extraction path, then uses it
to fix the defects that survive a live recording but fail a careful reading.

## Current state

`yt_dlp_plugins/extractor/tango.py` (613 lines) holds:

- `TangoTokenRefresher` — issues the 10-second CDN token, refreshes it late
  (`MARGIN = 2`), and swallows refresh failures.
- `TangoHlsRelay` — a loopback HTTP server in front of `cinema-*.tango.me`.
  Playlists are fetched with a fresh token; media segments are rewritten to
  absolute CDN URLs so ffmpeg fetches video directly.
- `TangoBase` — visitor check-in, token issue, alias resolution, `watch`,
  profile metadata.
- `TangoFeedIE` and `TangoIE` — `/live` and single-stream entry points.

Tests: `tests/test_relay.py` (playlist rewriting, stale replay, ENDLIST),
`tests/test_session.py` (visitor reset, `Tango-VT` expiry), `tests/test_urls.py`
(routing). 14 tests, all offline, all passing.

## Goals

1. `_extract_stream` runs end to end offline against captured API responses, for
   both the anonymous and the logged-in cookie jar.
2. Every HTTP request the extractor makes has a seam a test can drive, so a
   regression in resolve, watch, token or manifest handling fails a unit test
   rather than a 40-minute recording.
3. A recording that hits a token throttle, an upstream 404, or a 5xx control
   call degrades the way `PLAN.md` already claims it does.
4. The log stays readable during a long recording: no line per 8 seconds.
5. Errors name the actual cause: bad id, not live, ended, audio room,
   followers-only.
6. Output carries `timestamp`, so live recordings get an `upload_date` instead
   of nothing.

## Non-goals

- `--live-from-start` / DVR. The CDN serves an eight-segment, one-second sliding
  window with no DVR; the data is not there.
- Audio rooms. `watch` answers `FAILED_BAD_REQUEST`; whether they expose HLS at
  all is undecoded. Improve the message, do not support them.
- Live chat. `contrib/tango_chat_recorder.py` (moved 2026-11-21) still targets the undecoded
  `wss://gateway.tango.me/ws`.
- Proving the followers-only path. It needs an account that follows a currently
  live broadcaster.
- A scheduler or supervisor for unattended runs. See "Deferred".

## Anti-goals

- A test suite that needs the network, a live stream, or a Chrome profile to
  pass.
- Tests that re-implement the extractor's logic instead of driving it.
- More runtime dependencies. The plugin keeps working with a bare yt-dlp install.
- Changing the proven download path (relay rewrites, late refresh,
  cached-playlist replay) to make testing easier. The harness bends to the code
  here.

## Constraints

- The token lives ten seconds, so every playlist fetch re-issues one. The relay
  is mandatory: ffmpeg cannot refresh cookies.
- `watch` must precede any playlist fetch; it authorises the visitor.
- Only playlists need auth; segments are public.
- Rate limits are measured, not guessed: 400 token calls in 188 s and 200
  check-in-plus-token rounds in 191 s on one identity, none refused. Steady
  state is one call per ~8 s, so the headroom is large.
- Fixtures must contain no tokens, account ids or personal data.
- yt-dlp pins nothing: the plugin is imported by the installed yt-dlp on the
  operator's machine (Homebrew 2026.10.26 today). Anything used must exist in
  that version.

## Approaches

**A. Harden in place.** Fix each defect directly and judge it by live
recordings. Cheapest to start, but every check costs a live stream, the
regressions are invisible until they happen mid-recording, and a run that proves
nothing is indistinguishable from one that does.

**B. Add a recorder CLI first.** A small supervisor that retries, names files and
restarts on failure, aimed at unattended overnight runs. Useful, but it composes
on top of the extractor: it would hide exactly the failures this round is meant
to expose, and it is only worth building if unattended recording is wanted.

**C. Offline harness first, then harden.** Capture real responses as fixtures,
route the extractor's HTTP through a fake transport, and make `_extract_stream`
run without the network. Every later fix then has a failing test before it has a
live run. Costs one round of fixture capture and a new test module, and the
fixtures will drift when the API changes.

Chosen: **C then A**. C gives the harness; A's fixes land against it. B is
deferred until the operator says unattended runs are in scope.

## The how

### 1. Fixtures

`tests/fixtures/` holds captured bodies, one file per endpoint:

| File | Source |
|---|---|
| `check_in.json` | `POST /visitor-lobby/api/public/visitors/v1/check-in` |
| `alias_convert.txt` | `GET /visitors/profilealias/api/alias/convert?alias=…` |
| `stream_id.txt` | `GET /showroom-streams/api/public/stream/v1/id?broadcasterId=…` |
| `watch.json` | `POST /visitors/stream/client/v2/stream/{id}/watch` |
| `watch_wrong_id.json` | same, `code: FAILED_WRONG_STREAM_ID` |
| `watch_terminated.json` | same, `code: FAILED_TERMINATED` |
| `watch_bad_request.json` | same, `code: FAILED_BAD_REQUEST` |
| `profile.html` | `GET https://www.tango.me/stream/{id}` |
| `feed.json` | `POST /showroom-streams/api/public/web/catalog/v1` |
| `token.json` | `GET /stream/token-data/v1/web/visitor` |
| `master.m3u8` | CDN master playlist |
| `hd.m3u8` | CDN media playlist (best rendition) |
| `ld.m3u8` | CDN media playlist (worst rendition) |

Ids, aliases and CDN hosts are replaced with fixed synthetic values of the same
shape (22-character ids, `cinema-in41.tango.me` kept as a host). Names,
descriptions and thumbnails come from a public profile and are genericised.
Tokens do not appear: `token.json` carries a dummy token and an `expireAt` far
enough ahead that it never lapses mid-test. The refresh path is forced
explicitly by a test rather than by the clock.

Capture is part of this build, not an input: the operator's existing visitor
session and one live stream produce most files. Two states are rare — a live
audio room and a stream that ends during capture. Those two bodies are the
observed `{"code": …}` shapes from `PLAN.md`, marked as reconstructed in
`tests/fixtures/README.md` rather than captured. The README records the exact
request behind every file so a fixture can be re-captured by hand when the API
moves. A capture script is deferred until that has to happen twice.

Response headers (`Content-Type`, and the check-in's `Set-Cookie`) are not
bodies: they live in `tests/fake_net.py`'s route table, next to the file each
route serves.

Cookies are otherwise not fixture data. The visitor jar is produced by the
harness; a logged-in run is simulated by seeding the jar with the
`.www.tango.me` account cookies described in `PLAN.md`.

### 2. Fake transport

`tests/fake_net.py` fakes the network at yt-dlp's transport handler, not at
`urlopen`. It defines a `RequestHandler` for `http` and `https` that owns a
route table mapping `(method, host, path-without-query)` to a fixture plus
response headers, and returns a real `yt_dlp.networking.Response`. A test builds
its `YoutubeDL` with that handler first and yt-dlp's urllib handler behind it,
so nothing but the loopback relay reaches a socket, and an unrouted tango URL
raises instead of silently returning nothing.

The handler owns cookies because in yt-dlp the cookiejar lives inside handlers,
below `urlopen`: it reads `Cookie` from the downloader's jar and writes
`Set-Cookie` from the route table back into it. That is what lets a test observe
the check-in actually storing `Tango-VT`, rather than asserting on a canned jar.

Faking the handler rather than `_api_json` keeps the whole real path in play:
`_download_json`, `_download_webpage`, `_extract_m3u8_formats_and_subtitles`,
the cookie jar, the exception types, and the relay's own upstream fetch once
item 3 lands.

`tests/test_extract.py` builds a real `YoutubeDL` with
`{'quiet': True, 'skip_download': True}` and drives `TangoIE._real_extract` (and
`TangoFeedIE`) against it. Cases:

- anonymous stream id, alias and `@alias` all reach the same info dict;
- logged-in jar: account cookies survive, `Tango-VT` is added, the check-in is
  skipped when an unexpired token is already present;
- `timestamp` from `details.stream.duration`;
- each `watch` code maps to its own message;
- a profile with no live stream raises `UserNotLive`;
- an unreadable manifest names the followers-only possibility;
- the format's manifest URL is the loopback relay, and its media URL points at
  the CDN;
- the feed returns the captured entries.

The relay's own tests stay as they are and gain the new failure cases below.

### 3. Relay fetch through yt-dlp's HTTP stack

`TangoHlsRelay.fetch` currently calls `urllib.request.urlopen` directly. That
bypasses `--proxy`, `--socket-timeout`, yt-dlp's User-Agent and its own error
types, and it ignores `--no-check-certificates`. Replace it with
`self.ie._downloader.urlopen(yt_dlp.networking.common.Request(...))`, keeping
the same cookie, Origin and Referer headers. `urlopen` takes no timeout
argument and would otherwise wait forever; the request carries
`extensions={'timeout': 30}`, so the relay keeps the 30-second cap it has
today. The relay's retry policy stays: one retry with a forced refresh, then
cached-playlist replay, then a clean end.

Threading note: the fetch runs on a relay thread while the extractor thread may
be active. yt-dlp already shares its cookiejar across threads; a playlist GET
does not write cookies, so the added risk is small and is recorded as a known
assumption.

### 4. Proxy exemption for the loopback hop

With `--proxy`, yt-dlp gives ffmpeg `HTTP_PROXY`/`http_proxy` and ffmpeg would
send `http://127.0.0.1:<port>/…` to the proxy. The same happens with a shell
`HTTP_PROXY` and no `--proxy` at all, which is the common corporate setup.
Upstream fetches should be proxied; the loopback hop must not be.

The extractor therefore appends `127.0.0.1` and `localhost` to `no_proxy` and
`NO_PROXY` in the process environment, leaving existing entries in place, before
returning formats. Applied unconditionally rather than only when a proxy is
configured: detection has to cover `--proxy`, environment variables and the
macOS system settings, and an exemption on loopback is harmless when nothing is
proxied. It is a process-wide side effect and is documented under Limits.

Unit test: a pre-existing `no_proxy` keeps its entries and gains both loopback
entries; a second call does not duplicate them.

### 5. Ending a recording that the CDN has ended

A live media playlist can answer 404/410 when the broadcaster stops. Today that
raises, the relay answers 502, and ffmpeg fails the download even though the
file on disk is fine.

New rule: 404/410 on a playlist the relay has already served, or on a URI it
emitted from one it served, means the stream ended. The relay keeps that set:
`master_path` from construction, variant URIs when it serves a master, media
URIs when it serves a variant. For one of those, serve the cached body with
`#EXT-X-ENDLIST`, or, when nothing is cached, a minimal `#EXTM3U` media
playlist with `#EXT-X-ENDLIST`; cache the ended body so later reloads stay
ended. One warning, once, naming the end of the stream.

Anything else — the master before it ever loaded, or a path the relay never
emitted — still raises. That keeps a typo or a CDN reshuffle a visible failure
instead of a silently truncated recording.

This covers both "stream ended between playlist reloads" and "stream ended
before the first media playlist", and it makes the SIGINT path and the ended
path produce the same kind of file.

### 6. Retries on control calls

Alias conversion, the live-stream lookup, `watch` and the feed currently run
once, some with `fatal=False`. A transient 5xx therefore reads as
"No tango.me profile found".

Wrap them with the native `self.RetryManager()` (it honours `--extractor-retries`
and `--retry-sleep extractor`, both already user-facing). Retry transport errors
and 5xx only. Semantic answers arrive as HTTP 200 with a `code`, so a wrong id
is never retried, and a 404 from the live-stream lookup stays `UserNotLive`.

After the retries, these four are fatal: a control call that still answers 5xx
raises `ExtractorError(expected=False)`, because the site is at fault, not the
URL. The profile page is the exception and stays best-effort: it only supplies
title, alias and thumbnail, and aborting a working recording over decoration is
worse than a plain title. That inconsistency is deliberate. `_issue_token` keeps
its own policy: three attempts, one visitor reset, because that failure is
rate-limiting, not flakiness.

### 7. Quiet refresh

`TangoTokenRefresher.refresh` passes `note='Refreshing stream token'`, which
prints a line every ~8 seconds — about 450 lines an hour. Pass no note and use
`write_debug`, so the line appears only with `-v`. The relay keeps its
once-per-incident warnings.

### 8. A timestamp

`watch` returns `details.stream.duration` as milliseconds of elapsed live time
(4885285 in the captured response). When the stream is live, set
`timestamp = int(time.time() - duration / 1000)`, which gives yt-dlp an
`upload_date`. Accuracy is a few seconds: the field is a live counter read at
`watch` time. No timestamp for the finished-recording fallback, where `duration`
means total length rather than elapsed time.

### 9. Errors that name the cause

`_watch` maps the API code to an expected error:

| Code | Message |
|---|---|
| `FAILED_WRONG_STREAM_ID` | no tango.me stream has that id |
| `FAILED_TERMINATED` | the stream has ended |
| `FAILED_BAD_REQUEST` | tango.me refused the lookup; audio rooms are the usual cause |
| anything else | the code, quoted |

`_resolve_stream_id` distinguishes "no such profile" (empty alias conversion)
from "profile is not live" (`UserNotLive`). An HTTP 401/403 on `watch` mentions
the followers-only case and `--cookies-from-browser`.

### 10. Packaging

`pip install .` has never been run. Build a wheel, install it into a clean venv
that already has yt-dlp, and confirm `yt-dlp --list-extractors` contains
`tango` and `tango:live`. Fix `pyproject.toml` only if that fails.

## Verification

Offline, the gate for every change:

```
PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python \
    -m unittest discover -s tests
```

The suite must pass with no network, no Chrome profile and no live stream. A
missing fixture must fail loudly, not skip.

Live, after the harness is green:

- one anonymous recording ≥120 s by alias, one by `@alias`, one by stream id;
- one logged-in recording ≥120 s (`--cookies-from-browser chrome`), with the
  account cookies still in the jar;
- each file: `ffprobe` shows H.264 + AAC 720x1280, the log has no warnings
  (except deliberate ones), SIGINT finalises;
- the log has roughly one line per recording, not one per 8 seconds;
- `-F` on a live stream shows a `timestamp`/`upload_date` where there was none;
- a run with `--proxy` pointed at a live HTTP proxy starts and records,
  proving the loopback hop is exempt and the upstream fetch is not;
- a recording whose stream ends mid-run finishes with ENDLIST and a playable
  file;
- a wheel installed into a clean venv loads the plugin.

## Risks and open items

- **The fake handler drifts from the real one.** Cookie plumbing and header
  defaults are re-implemented in `tests/fake_net.py`. A yt-dlp upgrade can move
  them; the live smoke run is what catches it.
- **Fixtures drift.** A site change that keeps working live can still fail the
  offline tests. Accepted: the fixtures are dated, and live smoke runs stay part
  of the release check.
- **`timestamp` is approximate.** A few seconds. Stated in `README.md`.
- **`urlopen` on a relay thread.** Shares yt-dlp's cookiejar. Playlist GETs do
  not set cookies; recorded as an assumption, not proven.
- **`no_proxy` is process-wide.** Adding loopback entries can only stop a proxy
  from being used for loopback; it is visible to anything else in the same
  process.
- **Followers-only streams** remain unproven end to end, as in `PLAN.md`.
- **Audio rooms** stay unsupported, with a better message.
- **No DVR**, so `--live-from-start` stays impossible.

## Deferred

**Recorder CLI (approach B).** A supervisor that retries, timestamps filenames
and runs unattended. Build it only if recordings are meant to run without a
terminal attached; it changes nothing about the extractor and would be a second,
independent piece of work.

## Left to the builder

- File layout inside `tests/fake_net.py`, the route table's shape, and the
  handler's constructor wiring into `build_request_director`.
- Naming of the retry helper and where it sits in `TangoBase`.
- Whether the no-proxy adjustment belongs in `TangoBase` or in the relay's
  constructor.
- Whether the ended-playlist body is synthesised in `fetch` or in a helper next
  to `_end_playlist`.
