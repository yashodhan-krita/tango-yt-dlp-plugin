# Implementation notes

Design record for the tango.me extractor. Usage lives in `README.md`; this is
the decoded contract and the reasoning behind the code. It records the
2026-11-20 build; profile tracking and the review fixes are newer, and
`docs/kb/tango-me.md` is the up-to-date contract.

## API contract (verified against the live site)

Base: `https://www.tango.me/api`.

### Headers

Every API call sends `Origin` and `Referer: https://www.tango.me/`. The
`foreground-id`, `interaction-id` and `x-app-client-session-id` UUID headers
are sent too; requests work without them, so they are cheap camouflage rather
than a requirement.

`GET /stream/token-data/v1/web/visitor` **requires** `Referer`; without it the
API answers `400 Referer header is empty`.

### Visitor check-in

`POST /visitor-lobby/api/public/visitors/v1/check-in?appId=tango`, body `{}`,
`Content-Type: application/json`.

Sets three cookies on `www.tango.me`:

- `Tango-VT` — visitor JWT, valid ~30 days. The token endpoint needs it;
  without one it answers `401 Jwt is missing`.
- `Tango-SD` — base64 JSON: `sessionType`, `analyticsKey`, `accountId`,
  token expiry.
- `Tango-DeviceId` — random hex.

A logged-in Chrome profile carries `.www.tango.me Tango-SD` with
`sessionType: DEFAULT` **and a non-null `accountId`**, plus `Tango-ST`,
`Tango-RT`, `Tango-WST`. It has no `Tango-VT`, so check-in is still required
for a CDN token. Do not test "logged in" with `sessionType == 'ACCOUNT'`;
`accountId` is the reliable marker. The extractor only checks whether an
unexpired `Tango-VT` exists.

### Stream lookup

`POST /visitors/stream/client/v2/stream/{id}/watch?requestId=`, empty body.

It registers the viewer and returns the stream document. Corrected
2026-11-21: it is not an authorisation step. A fresh triplet gets 200 from the
master and a rendition with no prior `watch`; the 401 this note used to
record came from a stale token (see `docs/kb/tango-me.md`). It answers JSON
(`details.stream`, `details.settings`, `details.viewInfo.hlsStreamInfo`,
`viewerCount`, `status`, `anchor`), even when the site's own player asks for
protobuf.

Failure modes: `{"code": "FAILED_WRONG_STREAM_ID"}` with HTTP 200 for a bad
id, `FAILED_BAD_REQUEST` for some stream kinds (audio rooms).

### Aliases

- `GET /visitors/profilealias/api/alias/convert?alias=X` → the 22-char
  encrypted account id as plain text, or an empty body with HTTP 200 (or a
  404) when the alias does not exist or the value is an account id.
- `GET /showroom-streams/api/public/stream/v1/id?broadcasterId=<account>` →
  stream id, or HTTP 404 when the profile is offline.

### Feed

`POST /showroom-streams/api/public/web/catalog/v1?traceId=<uuid>` with
`{"size":30,"platform":"web_desktop","hMarketingCampaign":false}`. Returns
`catalogItems[].encryptedStreamId` plus broadcaster names.

### CDN

Hosts look like `cinema-in41.tango.me`. The `masterListUrl` from the watch
response is a master playlist with two renditions (1280x720 and 640x360,
relative `/v2/...` URIs, `EXT-X-TARGETDURATION:1`, eight-segment sliding
window, no `CODECS`, no DVR).

Auth is a cookie triplet on `.tango.me`:

```
tt=<token>; ttu=<username>; tte=<expireAt>
```

The relay sends the triplet as a plain `Cookie` request header and does not
rely on yt-dlp's jar: ffmpeg has no jar. Corrected 2026-11-21: the
`token-data` reply sets `tt`, `ttu`, `tte` (`Max-Age=10`) and `datadome`
itself, so they do reach the jar briefly; `_drop_bot_cookies` drops
`datadome` and `_dd_s` at each session step.

All three are required and `tte` must equal the token's `expireAt` exactly.
Query parameters (`?tt=`, `?token=`) do **not** authenticate. The token lives
ten seconds; `ttlSeconds` in the response is always 10 and cannot be raised
with a query parameter.

Media segments (`/v2/sh/.../*.ts`) and low-latency segments (`/v2/sl/...`)
need no auth at all. Only playlists (master, `hd`, `ld`, `preview`) do.

### Rate limiting

Two controlled probes, each on one fresh visitor identity:

- 400 token calls at 4/s (188 s): none refused.
- 200 rounds of check-in followed by a token at ~1/s (191 s): none refused.

So the plugin's steady state — one check-in, then one token per ~8 s — is far
inside the budget (a 1-hour recording is ~450 tokens).

The `403 Forbidden` seen during development was self-inflicted: A/B testing
spawned ~36 short-lived processes in a row, each importing the *same* logged-in
Chrome jar, so the same account identity checked in dozens of times within a
minute. In the failing moment a freshly created visitor got `200` from the
same address, so the refusal is keyed on identity, not on address or on token
frequency. `datadome`, `_dd_s`, analytics cookies and file-vs-browser cookie
jars were ruled out by A/B tests.

Recovery stays as it was measured: retry, then check in as a new visitor, and
replay the last playlist meanwhile.

## Design decisions

**Extract everything in Python, record with ffmpeg.** yt-dlp unconditionally
selects `FFmpegFD` for live HLS (`downloader/__init__.py`), and ffmpeg cannot
refresh cookies. Forcing `protocol = m3u8_native` does not help: `HlsFD` is
single-pass and does not loop for live streams.

**Loopback HLS relay instead of cookie injection.** `TangoHlsRelay` serves the
master and variant playlists with a fresh token on each request, and rewrites
media-playlist segment lines to absolute CDN URLs so the video path bypasses
Python. Child playlists stay on the relay because they need the token. The
token triplet travels as a request header; the relay never stores it in the
jar (the token reply's own `Set-Cookie` does, for ten seconds).

**No global cookie-jar monkeypatch.** An earlier version hooked
`cookiejar.add_cookie_header` to refresh the token before any request. It works,
but it leaks across extractors in a mixed playlist and refreshes without regard
to which stream is downloading. The relay owns refresh now.

**Refresh late.** The token has a 10-second TTL; `MARGIN` is 2 seconds, so a
refresh happens every ~8 seconds rather than every 6. That is a third fewer
calls to a rate-limited endpoint. A refresh is routine, so it is one debug
line (`--verbose`), not a progress line.

**Replay on throttle, end cleanly on exhaustion.** If the token cannot be
refreshed, serving the last playlist for up to two minutes is better than
aborting an hours-long recording; ffmpeg sees an unchanged playlist and
stalls until the token returns, then the recording continues. Past that, the cached media playlist is served with
`#EXT-X-ENDLIST`, so ffmpeg finalises a playable file instead of the download
failing.

**End when the CDN throws the playlists away.** A 404 or 410 for a playlist
the relay already served or emitted means the broadcast is over: the relay
serves the cached playlist with `#EXT-X-ENDLIST` appended (a minimal ended
playlist when nothing is cached), warns once, and keeps answering from the
cache, so ffmpeg finalises a playable file instead of the download failing.
The check runs before the retry decision, so an ended stream never burns a
token refresh, and a path the relay never served still raises: a bad URL
stays loud.

**`completeListUrl` stays for its narrow window.** Once a stream ends,
`watch` answers `FAILED_TERMINATED` with no details, the profile lookup 404s,
and the stream page carries no HLS URL, so a finished stream is unreachable in
practice. The coded `completeListUrl` fallback only covers a stream that ends
between `watch` and the manifest fetch; it stays for that window.

**Retry control calls, not semantic answers.** The four control calls (alias
conversion, live-stream lookup, `watch`, the feed catalog) run through
yt-dlp's `RetryManager`: a 5xx is retried and then reported as the site's
fault rather than a bad URL. A 404 on the alias and stream-id lookups is
accepted as the answer (`expected_status`), so a missing profile or an
offline broadcaster never burns the budget.

**Date live recordings from the duration.** The watch document reports how
long the broadcast has been running, so a live stream gets `timestamp`: now
minus that duration. yt-dlp derives `upload_date` from it. The value is
approximate to a few seconds; a finished recording gets no timestamp,
because the document does not carry its start.

**Exempt the loopback relay from proxies.** yt-dlp passes `--proxy` on to
ffmpeg as `http_proxy`, and ffmpeg applies it to every request it makes,
including the relay, so a proxied ffmpeg never reaches the relay and the
recording stalls. The extractor appends `127.0.0.1` and `localhost` to
`no_proxy` and `NO_PROXY` (extended, never overridden; idempotent), both
spellings because ffmpeg reads the lowercase one while other tools read the
uppercase. API and CDN traffic keeps using the proxy.

That environment variable covers ffmpeg only. Under `--proxy`, yt-dlp's own
proxy map is `{'all': proxy}` with no `'no'` key, and `select_proxy`
consults `no_proxy` only when `'no'` is present; its fetch of the relay's
master playlist would be tunnelled through a proxy that cannot reach this
machine. That fetch is made with `Request(url, proxies={'all':
'__noproxy__'})` instead. The relay's CDN fetches still use the proxy.

## Measurements

- Token budget: 400 token calls in 188 s and 200 check-in+token rounds in
  191 s, on one visitor identity, none refused. Steady state is ~7.5 calls a
  minute.
- 4-minute continuous recording: 239 s file, 29 token refreshes, no warnings,
  clean finalisation on SIGINT, H.264 + AAC, 720x1280.
- 72-second recording through a logged-in Chrome profile
  (`--cookies-from-browser chrome`, by profile alias): 9 token refreshes, no
  warnings, clean finalisation, H.264 + AAC, 720x1280. This is the path that
  used to fail about one run in three.
- 34-second anonymous recording after the CDN cookies stopped going into the
  jar: 6 refreshes, no warnings, clean finalisation, H.264 + AAC.
- Earlier 2:57 run without the relay stopped at 11 s with 401 (the failure the
  relay fixes).
- Cut a recording with SIGINT and yt-dlp runs `FixupM3u8`; the `.mp4` is
  playable.
- Packaging: `tango_yt_dlp_plugin-0.2.0-py3-none-any.whl` installs into a
  clean venv with yt-dlp 2026.10.26 and registered both extractors of the
  time (`['tango', 'tango:live']` through a `YoutubeDL` instance's
  `gen_extractor_classes()`). Since `TangoCdnIE` (2026-11-21) there are
  three: `tango`, `tango:cdn`, `tango:live`. yt-dlp loads plugins from the `yt_dlp_plugins`
  namespace package and does not scan entry points, and newer setuptools
  rejects the hyphenated `[project.entry-points."yt-dlp.extractor"]` group,
  so it is gone from `pyproject.toml`. `--list-extractors` prints before
  plugins load (`yt_dlp/__init__.py:969` vs `:980`), so it cannot list
  them.

## Open items

- A private/followers-only stream has not been tested end to end; the account
  cookie path is preserved for it.
- Audio rooms (no video) answer `FAILED_BAD_REQUEST` to `watch`; whether they
  expose HLS at all is not decoded.
- `contrib/tango_chat_recorder.py` still targets `wss://gateway.tango.me/ws`; the live
  chat WebSocket has not been decoded.
