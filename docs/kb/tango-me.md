# tango.me live streams: technical knowledge base

Last verified 2026-11-20 against the live site and yt-dlp 2026.10.26;
profile tracking, playlist authorisation and the session rules re-measured
2026-11-21. This note is the durable record: the
contract, the measured behaviour, the traps, and how to check each claim
again. `PLAN.md` is the shorter design record; `README.md` is for users.

## What this covers

`yt-dlp` records tango.me live streams through a plugin. It accepts a stream
id, a profile alias, `@alias`, `/profile/<alias>`, `/profile/<account-id>`,
the `/live` feed, and a CDN playlist URL copied from the browser
(`tango:cdn`). It works anonymously, or with
`--cookies-from-browser chrome` for followers-only streams. Output is
H.264 + AAC in MP4; the video bytes never pass through Python.

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
| Alias | `GET /visitors/profilealias/api/alias/convert?alias=X` | 22-char account id as plain text; empty body (200) or 404 when absent, including when the value is an account id |
| Stream id | `GET /showroom-streams/api/public/stream/v1/id?broadcasterId=<account>` | stream id, or 404 when offline |
| Feed | `POST /showroom-streams/api/public/web/catalog/v1?traceId=<uuid>` | `catalogItems[].encryptedStreamId` |

`GET /stream/token-data/...` requires `Referer: https://www.tango.me/`,
otherwise it answers `400 Referer header is empty`. `Origin` is sent too.
The `foreground-id`, `interaction-id` and `x-app-client-session-id` UUID
headers are optional camouflage; requests work without them.

`watch` is not an authorisation step. Measured 2026-11-21 on an empty jar,
with no `watch` call and no visit to the stream page: a fresh `tt` / `ttu` /
`tte` triplet from `token-data` gets 200 for the master and 200 for a
rendition; no cookie gets 401; a tampered `tt` gets 401. `watch` supplies the
stream document (status, `masterUrl`, anchor, metadata). An earlier note
said a valid token without a prior `watch` gets 401; the token in that run
was the static one embedded in the stream page, which had already expired
(it lives ten seconds). `test_a_copied_cdn_url_is_authorised_by_the_triplet_alone`
and `test_the_fake_cdn_answers_like_the_measured_one` pin the measured rule;
`FakeRH.require_watch` keeps the old rule available as a switch.

`https://www.tango.me/v2/<32 hex>/master.m3u8` answers 200 with `text/html`
(the single-page app), not a playlist; `m.tango.me` did not answer the same
path (2026-11-21). So the site host does not serve the CDN, and `TangoIE`
does not read that shape as an alias named `v2`
(`test_a_cdn_path_on_the_site_host_matches_nothing`).

### The HLS manifest

The watch response's `masterListUrl` is a master playlist with two
renditions. The manifest claims `1280x720` and `640x360`, but broadcasts are
often portrait and the file comes out `720x1280`; the manifest is not
corrected for rotation. It carries no `CODECS`, so `-F` prints `unknown` for
vcodec and acodec. The actual codecs are H.264 and AAC.

Variant order is not quality order. A master sampled on 2026-11-21 listed
1280x720 first and 640x360 second; one on 2026-11-20 listed the low variant
first. `-f 1` is therefore not a quality promise; use `-S res` or `-f best`.

`age_limit` is 18 on every result as a policy choice. Six catalog items
sampled on 2026-11-21 all had `isNsfw` false and `moderationLevel` 2, so the
payload gives nothing to derive it from.

Media playlists use `EXT-X-TARGETDURATION:1` and an eight-segment sliding
window with no DVR. There is no earlier data to fetch, so `--live-from-start`
cannot work. A recording starts at the live edge.

Every segment carries `EXT-X-PROGRAM-DATE-TIME` before `EXTINF`. Segment tags
belong to the segment that follows them; a merged playlist that hoists them
into the header parses as malformed in ffmpeg and playback falls to roughly
0.04x, with one segment fetched every 20 s.

The relay does not pass the eight-segment window through. It keeps the union
of recent playlists per stream, keyed by `EXT-X-MEDIA-SEQUENCE`, and serves up
to 45 segments. ffmpeg asks for the segment after the one it last played, so
without the union a reload that lands 8 s late skips whatever slid out;
ffmpeg logs `skipping N segments` and the file is short. The relay never
polls; it merges only when ffmpeg asks for a playlist, so the 45-segment
window covers ffmpeg running up to about 45 s behind the live edge, but a gap
of more than the CDN's ~8 s window between two reloads still loses the
segments in between. The window stays inside the CDN's segment retention
(below), so the oldest kept segment still returns bytes.

### Segment retention

Segments are public and cached: responses carry `cache-control: max-age=70`
and `via: 1.1 varnish`. The retention is real and short. A segment fetched
60 s after it appeared downloads in full (288204 bytes); the same URL 80 s
after it appeared answers `HTTP 200` with `content-length: 0` and no bytes.
That zero-length 200 is why the window cap is 45 rather than larger: a served
segment must still be within 70 s of its appearance when ffmpeg asks for it.

### Failure codes, not HTTP statuses

`watch` answers HTTP 200 with a semantic code:

| Code | Meaning | Usual cause |
| --- | --- | --- |
| `FAILED_WRONG_STREAM_ID` | No such stream | Typo or stale id |
| `FAILED_TERMINATED` | Broadcast over | Stream ended before the download started |
| `FAILED_BAD_REQUEST` | Lookup refused | Audio rooms are the known case |

A wrong id is never a retry. Treat 200-with-code as the answer.

## Profiles and tracking

The URL form decides what the identifier is:

| URL | Identifier | Resolution |
| --- | --- | --- |
| `/stream/<x>`, `/live/<x>` | stream id, or an alias by legacy | 22-char value is used as a stream id; anything else goes through the alias path |
| `/<alias>`, `/@<alias>`, `/profile/<alias>` | profile alias | alias → account id → current live stream id |
| `/profile/<account-id>` | account id | the alias conversion 404s, so the 22-char value is used as `broadcasterId` directly |

Stream ids and account ids are both 22-char base64url values, so the string
shape cannot tell them apart. The extractor never guesses: a profile URL is
always treated as a profile. A profile URL with an account id therefore works,
while the same value on `/stream/` is a stream id. When the alias lookup
rejects a 22-char value, it is used as `broadcasterId` and the "not live"
message says "treated as an account id", which makes a mistyped alias
diagnosable instead of an endless wait.

An offline profile is a semantic answer, not a failure: the stream-id lookup
answers 404 and the extractor raises `UserNotLive`. yt-dlp's
`--wait-for-video N` catches that exception and re-extracts every N seconds;
each retry resolves the alias again, so the stream id that appears later is
picked up. That is the tracking path:

```sh
yt-dlp --wait-for-video 60 -S res "https://www.tango.me/@<alias>"
```

The loop ends when a recording ends. A shell loop around it (with
`--download-archive`) tracks across separate broadcasts.

Identifiers are percent-decoded once before use, and both control responses
are accepted only when the body is shaped like a 22-char id; a gateway or
CDN error page reads as "no profile" / "not live" rather than travelling on
as an id. The `--wait-for-video` hint is left out when the caller already
set that option.

A profile URL means the current live stream, so a stream that ended between
resolution and `watch` raises `UserNotLive` (both `FAILED_TERMINATED` and a
non-`LIVING` status). That makes `--wait-for-video` re-resolve instead of
downloading the finished recording. A `/stream/<id>` URL keeps the recording
fallback.

Metadata comes from the page, not from guesswork:

- `tango.me/<alias>` renders a schema.org `ProfilePage` with `name`,
  `identifier`, `image`, `description` and a `FollowAction`
  `userInteractionCount`. It renders for offline profiles too.
- `tango.me/<account-id>` renders the same `ProfilePage` under the account's
  canonical address, with `identifier` set to the account id. The extractor
  reads it for account-id profile URLs, so followers and bio are not lost.
- `tango.me/@<alias>` and `tango.me/profile/<alias>` are SPA shells with a
  generic title; fetch the bare alias URL instead.
- `tango.me/stream/<id>` carries the live title (`Name (@alias) is Live on
  Tango`), which is the only alias source when the input is a stream id.

The extractor maps the profile page to `uploader`, `uploader_id`,
`channel_url`, `description`, `thumbnail` and `channel_follower_count`.

## Session rules

The extractor checks whether an unexpired `Tango-VT` cookie exists, and
checks in when it does not.

A logged-in Chrome profile is easy to misread: it carries
`.www.tango.me Tango-SD` with `sessionType: DEFAULT` **and** a non-null
`accountId`, plus `Tango-ST`, `Tango-RT`, `Tango-WST`, and no `Tango-VT`.
So "logged in" is `accountId` set, never `sessionType == 'ACCOUNT'`, and a
logged-in run still performs one visitor check-in to obtain a CDN token.

The check-in logs the account out. Measured on 2026-11-21 with a logged-in
Chrome profile: `POST .../visitors/v1/check-in?appId=tango` answers 200 and
sends `Set-Cookie` with `Expires=Thu, 01 Jan 1970 00:00:10 GMT` for
`Tango-WST` (Path=/), `Tango-SD` (Path=/) and `Tango-RT` (two path-scoped
copies), next to a fresh `Tango-VT` and `Tango-SD` with `Max-Age`. The
standard library removes the expired cookies from the jar, so
`GET /api/proxycador/api/public/v1/profiles/v2/me` goes from 200 before the
check-in to 401 after it. The browser shows the same:
`{meBefore: 200, checkIn: 200, meAfter: 401}`.

The extractor therefore snapshots `Tango-ST`, `Tango-RT`, `Tango-WST` and the
`.www.tango.me Tango-SD` before the check-in and re-adds any of them that the
reply removed (`_restore_account_cookies`;
`test_the_account_session_survives_the_check_in`,
`test_without_the_restore_the_check_in_logs_the_account_out`). The visitor
`Tango-SD` on `www.tango.me` is not snapshotted, and an anonymous jar gains
nothing (`test_an_anonymous_run_gains_no_account_cookies`). The token calls
leave the jar alone and carry the account cookies
(`test_token_calls_carry_the_account_cookies`). An earlier build took them out
for each token cycle and restored them afterwards, but that cycle runs on a
daemon relay thread every eight seconds or so, and a Ctrl-C in the middle
could save a `--cookies` file without them. The token request itself now
leaves the jar alone. The check-in still expires the account cookies for the
length of one request, and `_check_in` restores them in a `finally`. It runs
before the relay on the common path, and on the relay thread when a token
request answers 403 mid-recording (`_issue_token` forces a fresh visitor), so
a Ctrl-C inside that one request could still save a `--cookies` file without
them; the mid-relay case is `[needs live check]`.
`test_the_jar_never_lacks_the_account_during_an_extraction` throttles the
token once and checks the jar before every request, including those after
the forced check-in's restore. The 403 cure is
still a fresh visitor identity (`_reset_visitor`), and the account cookies may
let the endpoint issue an account-bound token, which a followers-only stream
may need. Whether the account identity on a token call changes the 403
recovery is `[needs live check]`.

A logged-in run therefore reaches `watch` with the account cookies in the
jar. Whether the site still accepts those restored cookies, and so whether
followers-only streams become visible, is `[needs live check]`: run with a
signed-in Chrome, `--cookies-from-browser chrome --cookies after.txt
--simulate`, then expect 200 from `profiles/v2/me` with `after.txt`. The
measurement above shows the check-in removes the cookies; it does not show
that restored ones work.

A fresh triplet authorises a playlist with no prior `watch` (measured
2026-11-21; see the API contract). `watch` gives stream metadata; it is not
an authorisation step, and `TangoCdnIE` skips it.

Check-in cookies are host-only for `www.tango.me`. If a fake or a test reply
sets `Domain=www.tango.me`, the standard library stores them as
`.www.tango.me` cookies instead, which changes which requests send them.

## Architecture

`TangoHlsRelay` is an HTTP server on `127.0.0.1` that fronts
`cinema-*.tango.me` for ffmpeg.

- Each playlist request fetches upstream with the current triplet in a plain
  `Cookie` request header: `tt=...; ttu=...; tte=...`.
- The relay does not depend on yt-dlp's cookie jar for the triplet; ffmpeg
  cannot read a jar anyway. The jar still receives it: `token-data` sets
  `tt`, `ttu` and `tte` with `Max-Age=10`, and `datadome`, through
  `Set-Cookie`. `_drop_bot_cookies` drops `datadome` and `_dd_s` at each
  session step (`_ensure_session`), so a `datadome` from one token reply
  rides on the next token call until then
  (`test_datadome_from_a_token_reply_rides_on_the_next_token_call`).
- Master playlists are served rewritten so their variant URLs point back at
  the relay, because variants need the token too.
- Media playlist segment lines are rewritten to absolute CDN URLs. Segments
  are public, so the video path skips Python and the relay sees roughly one
  request per second.
- The relay refreshes the token itself. `MARGIN = 2` seconds means a refresh
  about every eight seconds, not every six: a third fewer calls to a
  rate-limited endpoint.
- Only origin-form request paths are served. `@host/x`, `//host/x` and
  absolute URLs answer `400`, because concatenating them onto the upstream
  host could carry the token cookie to another host.
- The handler has a 30-second socket timeout, and cache, served-path and
  playlist-window state are mutated under one `RLock`; `ThreadingMixIn`
  serves each request on its own thread.
- `close()` sets a `threading.Event`; the reaper waits on it in 60-second
  steps, so a closed relay stops both threads instead of sleeping out the
  idle timeout.

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

The same rule rules out yt-dlp's native HLS downloader. The m3u8 parser marks
formats `protocol: m3u8_native`, and for a stream that is not live (a finished
recording) yt-dlp then picks `HlsFD`, which fetches the loopback relay through
the proxy and fails. Live HLS goes to ffmpeg whatever the protocol. The
extractor marks every relayed format `protocol: m3u8`, which resolves to
`FFmpegFD`, so a recording is downloaded by ffmpeg through the loopback relay
and an `http://` `--proxy` works (`test_relayed_formats_are_downloaded_by_ffmpeg`,
`test_the_proxy_rule_behind_the_ffmpeg_protocol`). Checked by hand on
2026-11-21: an 18 s recording with the change used ffmpeg and produced h264
720x1280 plus aac. `--downloader native` still selects `HlsFD` for a
recording; that is the user's choice, not the default, and it does not work
behind `--proxy`.

ffmpeg honours only an `http://` proxy. Measured: with a `socks5://` or
`https://` proxy, ffmpeg ignores it and fetches the CDN segments directly.
There is no fix in the plugin, and `--downloader native` is not an
alternative for a recording, because the native downloader tunnels the
loopback relay through the proxy.

This only shows up against a proxy that cannot reach loopback. A proxy running
on the same machine answers `127.0.0.1` happily and hides the bug; the live
check below therefore used one that refuses loopback.

## Degradation policy

The relay has three distinct end states. They are not interchangeable.

| Situation | Behaviour |
| --- | --- |
| Token refresh refused (403) | Reset the visitor identity after the first failure (check-in again), retry the token, keep replaying the last playlist for up to 120 s |
| Token still unavailable after 120 s | Serve the cached playlist with `#EXT-X-ENDLIST`; ffmpeg finalises a playable file |
| CDN answers 404/410 for a playlist already served | End the recording immediately: cached body (or a minimal ended playlist) plus `#EXT-X-ENDLIST`, warn once, answer later requests from cache. One 404 is enough: a 10-minute poll of a live media playlist on 2026-11-21 made 507 one-second requests, all 200, zero non-200, so no transient 404 has been seen and a two-404 confirmation is not implemented |
| CDN answers 404/410 for a playlist never served | Raise; a bad URL should stay loud |
| 5xx control call after retries | `ExtractorError(expected=False)`; the site is at fault, not the URL |
| 401/403 control call | Not retried (`_retry`). On `watch` with a visitor token reused from the jar: re-register once and retry (`test_a_401_with_a_jar_visitor_re_registers_once`); otherwise `ExtractorError(expected=True)` naming `--cookies-from-browser chrome` (`test_a_refused_watch_is_not_retried_beyond_the_re_registration`) |

A failed refresh starts a cool-down: 5 s before the next normal attempt, 2 s
before a forced one (`TangoTokenRefresher.COOLDOWN`, `FORCED_COOLDOWN`). It
applies only while the cached token is still unexpired
(`test_a_failed_refresh_cools_down_while_the_token_still_works`). With an
expired token there is no cool-down
(`test_an_expired_token_gets_no_cool_down`), so a long throttle pays a full
refresh cycle per playlist reload: about six token calls and two check-ins.

Two details matter:

- The 404/410 check runs before the retry decision, so an ended stream does
  not burn a token refresh.
- An already-ended cached body is returned at the top of `fetch`, before any
  upstream request. A test that seeds an ended cache therefore proves the
  short-circuit, not the throttle-replay branch. This was a real trap: the
  replay test passed while exercising the wrong path until the final review.

The `FAILED_TERMINATED` mapping is only reachable when the stream ended
before the download started. Tango removes a stream from its routes shortly
after the end: `watch` answers `FAILED_TERMINATED` with no details, the
profile lookup 404s, and the page has no HLS URL. A `/stream/<id>` URL keeps
a `completeListUrl` fallback for the short window in which `watch` still
returns the finished recording; once the route is gone, only "not live"
remains.

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
- `duration` in the watch document is elapsed broadcast time. Two
  `--simulate --print timestamp` runs 60 s apart on 2026-11-21 gave
  `<epoch>` and `<epoch>` minus one, so `timestamp` (now minus `duration`) is the
  broadcast start. yt-dlp's own `duration` field is `NA` for a live stream.

## Live verification (2026-11-20, merged build)

Every run below used the installed plugin and a real broadcast; durations are
`ffprobe`'s, not the wall clock.

| Run | Result |
| --- | --- |
| Stream id, 125 s cap | 115.6 s, 720x1280, H.264 + AAC, clean on SIGINT, no warnings. 1 check-in, 1 `watch`, ~14 token refreshes |
| `@<alias>`, `/profile/<alias>` for an offline alias | alias resolved, stream lookup ran, `The channel is not currently live` — the alias path is proven end to end |
| `--proxy` at a proxy that refuses loopback, 30 s cap | 29.4 s, 22 segments, zero loopback requests through the proxy, CDN traffic proxied |
| `--cookies-from-browser chrome`, 30 s cap | 3711 cookies read, one token 403, forced re-check-in, then 26.8 s of 360x640 video. The recovery path worked live; this run predates the cookie restore, which has not been re-measured on this path `[needs live check]` |

Two details from those runs:

- The feed's `broadcaster.alias` was null for all 30 live entries, so an alias
  cannot be discovered from the feed. Alias URLs still work; they are a legacy
  identity route, not the current one.
- A token 403 with a browser jar is expected once: the jar's identity is
  reused, and `_issue_token` resets the visitor and retries. The recording
  starts one or two seconds later; it is not a failure.
- The Chrome run picked the 360x640 rendition with `-f 1` because that master
  listed the low variant first. The manifest order is not quality order; a
  2026-11-21 sample listed 1280x720 first.

### Profiles and tracking (2026-11-20/21)

The profile path was verified live against one live broadcaster (`<alias>`,
account `<id>`, stream `<id>`; who it was is not recorded):

| Run | Result |
| --- | --- |
| `@<alias>`, `/profile/<account-id>`, `%40<alias>` | all resolve to the same stream id; display name, alias, account id and follower count all read from the profile page |
| Account-id profile URL after the metadata fix | returns the same follower count, `channel_id` and the bio from the account's `ProfilePage` |
| Offline profile (`/profile/<account-id>`) | `UserNotLive`, message names the profile and `--wait-for-video`; no relay is built |
| Recordings from `/profile/<alias>` and `@<alias>` | 37.6 s, 27.4 s, 23.4 s; 720x1280 H.264 + AAC; 0 skipped segments |

An account-id profile page is the same `ProfilePage` as the alias page, with
`identifier` set to the account id; reading it is what keeps followers and
bio for `/profile/<account-id>` inputs.

### Playlist window (2026-11-20, later)

The 42-minute run that ended by itself was 67 s shorter than its wall time;
the ffmpeg log showed 16 `skipping N segments` events, 23 segments lost. The
CDN's eight-segment window could not cover a reload that landed late. The
merged 45 s window was built to fix that, then measured:

| Run | Result |
| --- | --- |
| Three concurrent 720p, 12 min cap | 699/672/688 s recorded against 717 s wall, 0.94–0.97x each, 3–11 segments skipped; the link carried ~2.5x realtime for three 2.3 Mbit/s streams, so each fell behind by more than 45 s and then skipped. No token retry, no re-check-in, all three files playable |
| One 720p, 12 min cap | 714.3 s recorded against ~718 s wall, 1.00x, 715 segments, zero `skipping`, H.264 720x1280 + AAC. The ~4 s difference is the live-edge start |

Under load one run stayed inside the window: it trailed the live edge by 14 s
at the halfway point with zero skipped segments, which the eight-second
window could not have done. The skips that did happen all came after a run
had fallen more than 45 s behind, which is the cap working as designed: no
window helps once the link cannot carry realtime.

A first attempt served 0.04x and one segment every 20 s. `#EXTINF` and
`#EXT-X-PROGRAM-DATE-TIME` had been hoisted into the playlist header, so the
merged playlist listed all the segment tags before all the URIs. ffmpeg read
it as malformed. Segment tags must travel with their segment.

## yt-dlp issue #11433 (reviewed 2026-11-21)

<https://github.com/yt-dlp/yt-dlp/issues/11433> asks for tango.me support and
is still open. Every comment was read and the claims were probed live on
2026-11-21. What it establishes:

1. **The reported failure is generic extraction.** In 2024 tango redirected a
   client without JavaScript to `/stream/noscript.html`; yt-dlp followed the
   redirect and reported `Unsupported URL`. This plugin never reads that page,
   so the redirect cannot break it. The server redirect is gone (the URL now
   answers 200 directly), but the route still exists, and `watch` with id
   `noscript.html` answers HTTP 400 `Unexpected server error: Illegal base64
   character 2e`. Before this review the plugin read it as an alias and
   reported `No tango.me profile found for "noscript.html"`. `TangoIE` now
   names the no-JavaScript page for `/stream/noscript.html`,
   `/live/noscript.html` and `/noscript.html` and raises before any request.
2. **The CDN path hash is not the stream id.** The issue notes that "this ID
   seems differ". `watch` returns the stream's own 22-char id and
   `hlsStreamInfo.masterUrl` at
   `https://cinema-in41.tango.me/v2/<32 hex>/master.m3u8`. The extractor reads
   `masterUrl`; building the path from the stream id would 404.
3. **`?token=` alone is not authentication.** The master URL answers 401 with
   `?token=<token>&username=<user>` and no cookies, 401 with nothing, and 200
   with the `tt` / `ttu` / `tte` cookies only. That is why a pasted
   `master.m3u8` link "stops after 2-3 minutes", and why this plugin sends the
   triplet as cookies.
4. **`token-data` sets the triplet itself.** Its response sets `tt`, `ttu` and
   `tte` with `Max-Age=10`, matching the page's ten-second poll. It also sets
   `datadome`, which this plugin drops at the next session step
   (`_DROP_COOKIES`, see Architecture).
5. **`watch` answers protobuf unless asked for JSON.** Send
   `Accept: application/json`, as `_api_headers` does.
6. **The playlist file name is not validated.** Any `*.m3u8` under
   `/v2/<hash>/` is the same playlist for that hash: the master hash returns
   the variant list, a rendition hash returns that rendition's media
   playlist. A real master lists renditions as
   `/v2/<hash>/live.m3u8?m=<master hash>&`, and those answer 200 with or
   without `?m=`. `TangoCdnIE` (`tango:cdn`) accepts a copied playlist URL
   under any name, including `live.m3u8`, and relays its path unchanged
   without the query.

On the issue's own inputs the plugin now answers: the dead stream id gives
`The stream has ended`, and the issue's alias (`<alias>`, no longer
registered) gives `No tango.me profile found for "<alias>"`. Both are
correct.

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
  expect `tango`, `tango:cdn` and `tango:live`.
- The wheel (`pyproject.toml`, `setuptools.build_meta`) holds only
  `yt_dlp_plugins/` plus metadata; `contrib/`, tests and docs are not
  shipped. Built from a clean copy on 2026-11-21.
- A pip install is visible only to the interpreter that runs yt-dlp. A
  pip install into another environment succeeds and yt-dlp never loads it.
- `install.sh` symlinks `yt_dlp_plugins/` into
  `~/.config/yt-dlp/plugins/tango-yt-dlp/`, and refuses to run when that
  link path is a real directory, because `ln -sfn` would create the link
  inside it. Its discovery check (`--flat-playlist` on `/live`) makes two
  requests to tango.me: a check-in and the catalog. A bare
  `~/.config/yt-dlp/plugins/extractor/` directory is a stale layout yt-dlp
  does not load; it should not exist next to the symlink.
- `install.sh` pipes yt-dlp into `grep`. Without `-q`, grep reads all input;
  with `-q` it exits early, yt-dlp dies of SIGPIPE, and `pipefail` reports a
  false "plugin did not load".

## Test harness notes

Gate command (102 tests on 2026-11-21):

```sh
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
  /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
```

`XDG_CONFIG_HOME` is not optional on a machine with the plugin installed.
`import yt_dlp` installs yt-dlp's plugin finder, which serves
`yt_dlp_plugins` from `~/.config/yt-dlp/plugins/*` ahead of `PYTHONPATH`, so
the suite would test the installed copy. `tests/test_environment.py` fails
when that happens.

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
  `tests/fixtures/README.md` records which is which. The two
  `profile_page*.html` fixtures mirror the live three-block JSON-LD
  (Organization, WebSite, ProfilePage) rather than a byte capture.
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
yt-dlp -v -S res "https://www.tango.me/<alias>"
```

Expected in `-v`: one check-in, one `watch`, a token about every eight
seconds as a debug line (not a progress line), and relay requests to
`127.0.0.1`. For a copied CDN link, `-v` also prints the URL as given,
including its `token=` and `username=` values; do not paste that log.

If a playlist 401s immediately, the token is stale (more than ten seconds
old, or the static one from the stream page) or the `tte` value does not
equal `expireAt`. A missing `watch` is not the cause (measured 2026-11-21). If a playlist fetch stalls behind
a proxy, check that `no_proxy` reached the ffmpeg process. If a proxied run
fails before playback with "HLS manifest could not be read", the relay's
master fetch went through the proxy: it needs its per-request
`__noproxy__` bypass.

## Open questions

- A stream that ends mid-recording was caught once: the relay logged "The
  stream has ended (the CDN answered 404)", ffmpeg exited 0, and the file was
  playable. That run was 67 s short of its wall time, which is what led to the
  merged playlist window; the window is now verified, but the end-during-a-
  recording path has not been re-observed with it. The 404/410 ending also
  stays covered offline.
- Followers-only and private streams are implemented but not proven end to
  end; that needs an account that follows a live broadcaster. Three catalog
  pulls (60 entries) contained no `isPrivateStream: true` entry. Whether the
  restored account cookies are accepted, and whether carrying them on the
  token calls changes the 403 recovery, is `[needs live check]` (Session
  rules).
- Alias case: settled 2026-11-21. Every case variant of an alias resolved
  to the same account id, so the extractor passes it through unchanged.
  Route names (`/Live`, `/Stream/`) are matched case-insensitively
  (`test_route_names_ignore_case`).
- The two profile page fixtures follow the live JSON-LD shape but are
  scrubbed reconstructions. A literal capture would pin the exact block
  order and field set.
- Segment retention is measured from one CDN edge (`cinema-in41`): full at
  60 s old, zero-length at 80 s. Other edges may differ, which is why the
  window cap leaves 25 s of margin rather than sitting at the retention edge.
- Audio rooms answer `FAILED_BAD_REQUEST` to `watch`. Whether they expose HLS
  at all is undecoded.
- `contrib/tango_chat_recorder.py` still targets `wss://gateway.tango.me/ws`
  and is not used by the extractor; the live chat WebSocket has not been
  decoded.
- The relay closes its server on extraction failures, but after a successful
  extraction nothing closes it until the 900-second idle timeout (measured
  on a monotonic clock) or process exit. `-F`, `--simulate` and `--print`
  leave a listening socket and two daemon threads per URL for that long; the
  port is OS-assigned, so nothing else is blocked. Deferred (F13 in the
  adversarial plan).

## Sources

- `PLAN.md` — decoded contract, measurements, design decisions.
- `docs/superpowers/specs/2026-11-20-tango-hardening-design.md` — the
  hardening round's intent and constraints.
- `tests/fixtures/README.md` — fixture provenance.
- `~/.ai-audit` ledger entries for 2026-11-19 and 2026-11-20 — session
  records, including the failures that produced these rules.
