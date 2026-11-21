# Adversarial hardening plan

Date: 2026-11-21
Branch: `fix/cdn-url-and-account-session` (8 modified files, uncommitted)
Scope: review and plan only. No source, test, README or existing doc was edited.

## State summary

The plugin has three extractors (`tango:live`, `tango`, `tango:cdn`), a loopback
HLS relay with a merged playlist window, and a cookie snapshot/restore around
the visitor check-in. The 63 offline tests pass. The uncommitted diff adds
`TangoCdnIE` for copied CDN playlist URLs, moves relay construction into a
shared `TangoBase._relayed_formats`, and restores the account cookies that the
check-in reply expires. The relay itself holds up under review: no thread or
socket outlives `close()`, a double `close()` is safe, the three extractors
claim disjoint URL sets, and the profile and stream resolution chain behaves as
the handoff says.

The risk sits in the two uncommitted behaviours and in the suite that is meant
to guard them. `TangoCdnIE` never calls `watch`, which the KB says every
playlist fetch depends on, and no live run of it exists. The cookie restore puts
the account identity back on every token request, including the forced
re-registration that was the measured recovery from a 403; the one live
measurement of that path predates the restore. The suite cannot tell either
way: it imports whichever plugin copy yt-dlp finds first (the installed one,
not the checkout), and its fake CDN and token routes accept any request. Of 27
deliberate mutations of `tango.py`, 16 pass the suite once the import problem
is removed (Appendix A). Findings: 1 critical, 4 high, 6 medium, 21 low.

## How the evidence was produced

All commands ran outside the repo working tree, in `/tmp/adv`, and the repo
status was identical before and after (the same 8 modified files, plus this
file).

- Full suite once, with the interpreter named in the task.
- Mutation harness: copy `yt_dlp_plugins/` and `tests/` to a temp dir, apply one
  textual change to `tango.py`, run the suite with
  `XDG_CONFIG_HOME=<empty dir> PYTHONPATH=.` so the copy is the code under test.
- Direct experiments against the real classes: VOD-length playlist through
  `_MediaWindow`, a client that disconnects mid-response, double `close()`,
  malformed `Tango-VT` payloads, URL ownership across the live extractor
  registry, token-failure request counts.
- A wheel build from a copy, and a plugin-directory load test with a second
  namespace plugin.

Not read: `docs/superpowers/specs/2026-11-20-tango-hardening-design.md` (outside
the task list). `docs/superpowers/plans/2026-11-20-tango-hardening.md` was read
for its header, constraints and closing section only; it is marked historical.
Nothing here was measured against the live site.

## Findings

Severity counts: critical 1, high 4, medium 6, low 21.

Evidence tags: `M<n>` is a mutant in Appendix A; `X<n>` is an experiment in
Appendix B; `L<n>` is a live check in the "Needs a live check" section.

| ID | Sev | File:line | What is wrong | Evidence | Fix | Changes |
| --- | --- | --- | --- | --- | --- | --- |
| F01 | critical | `tango.py:1127-1156`, `docs/kb/tango-me.md:50-52`, `README.md:108-119` | `TangoCdnIE` checks in and fetches a token but never calls `watch`. The KB records that a valid token without a prior `watch` still gets 401 from playlists. The reasoning in the handoff ("the token endpoint is not stream-scoped") covers the token, not playlist authorisation. The README tells users the copied link "is now accepted as-is". | No live run of `tango:cdn` exists. The offline test cannot fail on auth: the fake CDN returns the playlist for any request (`fake_net.py:107-117`); M3 (no check-in) and M17 (no token) both pass. | Run L1. If a cold visitor gets 200, keep the design and make the fake enforce cookies (F04). If it gets 401 without `watch`, the extractor cannot work from a path alone: change its error to say so, point to the stream URL, and withdraw the README claim. | Behaviour if L1 fails, else tests and wording |
| F02 | high | `tests/` (all), `tests/fake_net.py:182-189` | The suite does not test the checkout. `import yt_dlp` installs yt-dlp's plugin finder, which serves `yt_dlp_plugins` from `~/.config/yt-dlp/plugins/*` ahead of `PYTHONPATH`. On a machine with the symlink install, a worktree, a PR checkout or a CI clone tests some other copy. | X1: with the install present, `inspect.getsourcefile(TangoHlsRelay)` returns the config-dir path while cwd holds a different copy. Every mutant in a `/tmp` copy passed, including ones with direct tests (M12 removes the 404 ending). With `XDG_CONFIG_HOME` pointed at an empty dir the same mutant fails 2 tests. Here the symlink targets this repo, so today's green run is valid by coincidence. | Add `tests/__init__.py` that asserts `os.path.realpath(tango.__file__)` lies under the repo root and fails loudly otherwise. Document the command with `XDG_CONFIG_HOME="$(mktemp -d)"`. | Tests only |
| F03 | high | `tango.py:633-656`, `658-672`, `683-704` | The restore re-adds the account cookies after every check-in, including the forced re-registration inside `_issue_token`. Before the change the check-in reply stripped them, so a forced check-in produced a fully fresh identity. The KB attributes the 403 to identity throttling and the cure to a fresh identity. With the restore, the account identity rides on every token call and the cure may no longer shed it. The first token after `watch` also now carries account cookies, which was never measured. | Code path. The live evidence for recovery (KB:311, one 403 then 26.8 s of video) predates the restore. KB:180-185 and README:158-163 state the benefit as fact; nothing measures that `watch` accepts the restored cookies. | Run L2 and L3. If recovery regresses: snapshot before the forced check-in, make the token calls without the account cookies, restore in a `finally` at the end of `_issue_token` so the jar is right for the next URL. | Behaviour, conditional on L2 and L3 |
| F04 | high | `tests/fake_net.py:88-117`, `tests/test_extract.py:179-202` | The fake does not enforce the contract the KB documents: the token route does not require `Tango-VT`, the CDN routes ignore the cookie triplet, `watch` is not required before playlists, `Accept: application/json` and `Referer` are not checked. The CDN extractor test therefore proves only that formats come back. | M3, M4, M17, M26, M27, M28 survive. The token fixture expires in 2100 (`fixtures/token.json`), so refresh logic never runs end to end. | Make the fake strict: 401 unless the request carries the expected cookies, `Referer` and `Accept`; track whether `watch` ran for the visitor and answer playlists accordingly (switch set by the L1 result). Add the tests in task T2. | Tests only |
| F05 | high | `tests/test_session.py`, `tests/test_extract.py` | Nothing exercises the recovery path that keeps a long recording alive: 403 on the token endpoint, forced check-in, success on retry. The margin check in `ensure` and `_drop_bot_cookies` are also unpinned. | M9 (no visitor reset), M10 (no forced check-in), M13 (bot cookies kept), M15 (never refresh unless forced) all survive. `test_session.py:46-60` tests `_reset_visitor` in isolation only. | Add the tests listed in T2 (403 then 200 route, expiring token, datadome in jar). | Tests only |
| F06 | medium | `tests/fake_net.py:75-87`, `tests/test_extract.py:360-373` | The cookie restore test is partly vacuous. The fake expires host-only copies, so only the host-only `Tango-RT` is removed; `Tango-WST` and `Tango-SD` sit on `.www.tango.me` and are never touched, and `Tango-ST` is not expired at all. The KB records two path-scoped `Tango-RT` copies; the test has one. | M1 (restore does nothing) is caught, but M2 (snapshot only `Tango-RT`) and M21 (snapshot every `Tango-SD`) survive. | Expire each cookie the way the live reply does (host-only `Tango-WST`, `Tango-RT` at two paths), assert the removal happened before the restore (precondition), and keep one `.www.` cookie the reply cannot touch to prove it is left alone. | Tests only |
| F07 | medium | `tango.py:236-243`, `359`, `871-880`, `941-947` | The `completeListUrl` fallback (a `/stream/<id>` URL for a finished stream) goes through the same 45-segment window and prunes the recording to its last 45 seconds. With `live=False` yt-dlp also picks its native HLS downloader, which fetches the relay URL through `--proxy`; the `__noproxy__` bypass covers only the one master fetch. | X2: a 300-segment playlist with `#EXT-X-ENDLIST` comes out of `_MediaWindow` with 45 segments. The branch has never run live (KB:275-281). | Give the relay a `live` flag, skip pruning when it is false, and force the ffmpeg protocol for relay formats so the proxy exemption applies. | Behaviour |
| F08 | medium | `tango.py:104-111`, `417-426`, `683-704` | A failed refresh has no cool-down. `cookie_header()` calls `ensure()`, which retries the whole `_issue_token` (3 token calls, 1 forced check-in, 3 s of sleeps) while holding the refresher lock; the upstream attempt then 401s and `fetch` calls `ensure(force=True)`, which repeats it. Every playlist request during a throttle costs two full failing refreshes, and all handler threads queue behind the lock. | X3: one failing refresh made 3 token calls, 1 forced check-in and slept 1 s then 2 s. Two refreshes per request follows from the code path. | Record a failure time in the refresher and skip network work for a short, growing interval (a few seconds) unless the cached token is still valid; do not force when a refresh just failed. | Behaviour |
| F09 | medium | `tango.py:439-453` | One 404 or 410 for a playlist the relay served ends the recording for good: the ended body is cached and later requests short-circuit (`410-411`). A transient CDN 404 (edge miss, cache churn) would finish a long recording with no retry. | Code path. KB:503-508 saw one genuine end; nothing measures how often a live stream 404s transiently. | Run L6. If transient 404s occur, require a second 404 a few seconds later before ending. If none occur, leave it and record the measurement. | Behaviour, conditional on L6 |
| F10 | medium | `tango.py:620-631`, `658-672`, `789-795` | `_has_visitor_token` trusts the JWT `exp` only. A `Tango-VT` that is unexpired but rejected by the server (rotated, revoked, copied from an old cookie file) skips the check-in; `watch` then answers 401 or 403 and the error says to use `--cookies-from-browser chrome`, which is wrong for a stale visitor. `_issue_token` self-heals once; `_watch` does not. | Code path; plausibility not measured (L7). | In `_watch`, on 401 or 403 when the visitor token was taken from the jar and not issued this run, call `_ensure_session(force=True)` once and retry before raising. | Behaviour, conditional on L7 |
| F11 | medium | `README.md:134-142`, `install.sh:45-54` | The README says `-f 1` is the 720p rendition. The KB records that the manifest order is not quality order (a master listed the low variant first, so `-f 1` gave 360x640). | KB:321-322 against README:142. | Say `-S res` (or `-f best`) and keep `-f 1` out of examples; confirm with L8. | Wording |
| F12 | low | `tango.py:1011`, `1153` | `age_limit` is 18 for every stream and for the copied-URL path. It is not derived from anything and not documented. The watch document carries `nsfw` and `moderationLevel` (`fixtures/watch.json`). `--age-limit 17` silently skips every Tango stream. | grep: no mention in README, KB, PLAN or handoff. | Decide from L9 whether `details.nsfw` should drive it; document whichever rule stands. | Behaviour or wording |
| F13 | low | `tango.py:349`, `385-397`, `898-913` | After a successful extraction nothing closes the relay; it lives until 900 s of idle time or process exit. `-F`, `--simulate`, `--print` and long batch runs leave a listening socket and two threads per URL for up to 15 minutes. The KB (525-527) calls it a "held port", but the port is OS-assigned, so only the socket and threads linger. | X4: after `close()` no thread remains, so the leak is time-bounded, not permanent. | Close the relay if no request after the manifest read arrives within about 2 minutes. | Behaviour |
| F14 | low | `tango.py:556-573`, `778-795` | `_watch` retries 401 and 403 three times before printing its hint. They are not transient, and each attempt re-registers the viewer. | The suite log prints "HTTP Error 401: Unauthorized. Retrying (3/3)" during `test_a_401_points_at_the_logged_in_retry`. | Do not retry 401 and 403 in `_retry` for control calls. | Behaviour |
| F15 | low | `tango.py:620-631` | `_has_visitor_token` raises on a JWT whose payload is valid JSON but not an object (`AttributeError`) or whose `exp` is a non-numeric string (`ValueError`). A bad cookie aborts the run before check-in. | X5: a list payload raises `AttributeError`; `{"exp": "soon"}` raises `ValueError`. | Treat any parse or type failure as "no usable token" and check in. | Behaviour |
| F16 | low | `tango.py:393-397` | The idle reaper compares `time.time()` values. A laptop that sleeps longer than 900 s mid-recording closes the relay within a minute of waking. | X4 confirmed the wall-clock source. A monotonic clock does not advance across sleep on macOS. | Use `time.monotonic()` for `_last_request` and the age check. Keep `time.time()` for the token expiry, which is server epoch time. | Behaviour |
| F17 | low | `tango.py:286-297`, `309-311` | A client that disconnects mid-response (ffmpeg stopped with Ctrl-C) makes `BaseServer.handle_error` print a `BrokenPipeError` traceback to stderr. | X6: reproduced with a client that resets after sending its request. | Override `handle_error` on `_TangoRelayServer` to ignore `ConnectionError` and `BrokenPipeError`. | Behaviour (quieter) |
| F18 | low | `tango.py:596-606`, `622`, `638-641`, `650-656` | The refresher thread iterates and mutates the shared cookie jar without the jar's lock while relay handler threads run `urlopen`, whose response handling also writes the jar. A concurrent write can raise "dictionary changed size during iteration"; `ensure` would catch it and print a refresh warning. | Code path only; the window is small and not reproduced. | Wrap jar iteration in `jar._cookies_lock` (private, long-stable) or retry once on `RuntimeError`. | Behaviour |
| F19 | low | `tango.py:69-71`, `666`; `docs/kb/tango-me.md:198-199`, `389-391` | The module docstring and KB say the triplet is never in the jar. The KB's own measurement says `token-data` sets `tt`, `ttu`, `tte` (Max-Age 10) and `datadome` itself, so yt-dlp stores them. `_drop_bot_cookies` runs only in `_ensure_session`, so `datadome` stays in the jar after the first token and may be written to a `--cookies` file. | KB:389-391 (measured) against the docstring; code path for the persistence. L10 settles it. | Reword the docs; call `_drop_bot_cookies` after each token response if L10 shows `datadome` persisting. | Wording, maybe behaviour |
| F20 | low | `tango.py:95`, `968-969` | `username` from the token reply is placed unquoted in the master URL query and the cookie header. A value with `&`, `#`, `+` or a space would corrupt the URL. | Fixture value is plain ASCII; real values not sampled (L11). | `urllib.parse.quote(username, safe='')` in the query; leave the cookie value as measured. | Behaviour only if L11 finds odd values |
| F21 | low | `tango.py:1062-1066` | `TangoIE` matches `https://www.tango.me/v2/<32 hex>/master.m3u8` (and the apex host) as an alias named `v2`. The user gets a check-in, an alias lookup, then `No tango.me profile found for "v2"`. The URL test pins only that `TangoCdnIE` does not claim it. | Registry run (X7): `tango` claims it. Real CDN hosts are `cinema-*`, so this needs a hand-edited URL. L12 checks whether `www` serves `/v2/`. | Add `(?!v2/[0-9a-fA-F]{32}/)` to `TangoIE`; if L12 shows `www` serves the CDN, let `TangoCdnIE` take `www` and `m` instead. | Behaviour (error text) |
| F22 | low | `tango.py:1028`, `1062-1066` | Reserved routes and the `kind` prefix are case sensitive, so `/Live` is an alias lookup and `/Stream/<id>` is read as alias `Stream`. The site folds alias case (handoff:325-327). | X7: `/Live` and `/LIVE/` are claimed by `tango`, not `tango:live`. | Make the lookahead and `kind` group case-insensitive with an inline `(?i:...)` group. | Behaviour |
| F23 | low | `docs/kb/tango-me.md:309`, `326-334`, `404-405`; `README.md:36-40`; `tango.py:1068-1086`; `tests/test_urls.py:15-33`; `docs/superpowers/plans/2026-11-21-issue-11433-followups.md:24-26` | Real-looking broadcaster aliases, account and stream ids, a display name and a follower count sit in the KB, README, `_TESTS` and a test. They identify a third party. | Read of each file. | Replace with the fixed fake ids from `tests/fake_net.py` and `<alias>` placeholders; keep the measurements, drop who was measured. | Wording and test data |
| F24 | low | `docs/kb/tango-me.md:418-419`; `PLAN.md:207-209`; `docs/handoff-2026-11-21-tango-plugin.md:3-6`, `99-114`; `README.md:257-271` | Stale statements: registry expected as `['tango', 'tango:live']` (now three); the handoff says working tree clean with 59 or 60 tests (now 63 and uncommitted changes); the architecture block omits `TangoCdnIE` and `_relayed_formats`; the file list omits `fixtures/` and `PLAN.md`. | Suite output, registry run X7. | Update in the docs commit, after code changes settle the counts. | Wording |
| F25 | low | `tango.py:352-354`; `README.md:200-203` | The throttle replay is said to cost "duplicated segments". The window is keyed by media sequence, so ffmpeg sees an unchanged playlist and waits; the loss comes when the token returns after more than the retention window. No measurement backs either word. | Code path. | Say "stalls until the token returns" and keep the 120 s bound; confirm in L5-style run if a throttle is ever caught live. | Wording |
| F26 | low | `docs/kb/tango-me.md:183-185`; `README.md:158-163`; handoff:124-129 | The restore is documented as what "makes followers-only streams visible". The measurement shows the check-in removes the cookies, not that restored cookies are still accepted. The README concedes the path is unproven; the KB and handoff do not. | L2. | Add `[needs live check]` to those sentences until L2 passes. | Wording |
| F27 | low | `README.md:89-92` | `timestamp` is derived from `details.stream.duration`, assumed to be elapsed broadcast time in milliseconds. The test reuses the fixture value, so it is circular. | PLAN.md:168 asserts it; no measurement. | L13. | Wording unless L13 disagrees |
| F28 | low | `README.md:9`, `pyproject.toml:7-8` | "yt-dlp 2024 or newer" and `requires-python >=3.9` are claims. Only 2026.10.26 was tested. The code needs `Request(extensions=...)`, `proxies=` per request, `UserNotLive` and `_yield_json_ld`. | Suite run on one version. | L14, then set the floor to what passes. | Wording or metadata |
| F29 | low | `pyproject.toml`, `install.sh:36-39`, `README.md:12-29` | Packaging gaps. There is no `[build-system]` table (the build worked with setuptools defaults). The README never explains `pip install`, and a pip install into the wrong environment (Homebrew or pipx yt-dlp) is invisible to yt-dlp. `install.sh` says the check is "one request" but `/live` costs two (check-in, catalog), and `ln -sfn` creates a nested link if the destination is already a real directory. | X8: the wheel holds only `yt_dlp_plugins/` (two empty `__init__.py`, `tango.py`) plus metadata; the chat recorder, tests and docs are not shipped. | Add `[build-system]`, document `pipx inject` and the symlink alternative, guard the `ln` target. | Wording and metadata |
| F30 | low | `tango_chat_recorder.py:40`, `58-60`, `145`; `README.md:271` | The recorder targets the retired gateway (the KB calls that API dead code), passes `extra_headers` (renamed `additional_headers` in current `websockets`), stamps every binary message with the CLI username, and offsets subtitle times from the first message instead of the recording start, so they cannot line up with the video. It is listed in the README file table. | L15 for the keyword error; the rest by reading. | Move it to `contrib/` or delete it; if kept, mark it unusable in the README table. | Wording |
| F31 | low | `tests/test_relay.py:82-93`, `tests/test_extract.py` | The HTTP handler is exercised only through the master fetch. `HEAD`, the 502 path and response headers have no test, and `PathValidationTest` calls `fetch` directly. | M24 (proxy exemption call removed from `_relayed_formats`) and M19 (served set never updated) also survive. | Add one test that GETs and HEADs a relay over a socket; one that checks `no_proxy` after `_relayed_formats`; one that 404s a media playlist fetched directly. | Tests only |
| F32 | low | `tango.py:361-364`, `421-426`, `959-969` | The relay sends the token cookie to whatever host the `masterUrl` names. For the `watch` path that host is server-supplied. A hostile or corrupted response could send the visitor token elsewhere. The copied-URL path is already limited to `*.tango.me` by its regex. | Code path; low impact (visitor token, ten seconds). | Reject a master host that does not end in `.tango.me` in `TangoHlsRelay.__init__`. | Behaviour |

## Not a bug, but worth knowing

Checked and fine:

- Relay shutdown. `close()` leaves no thread (X4); calling it twice is safe;
  `shutdown()` before `serve_forever()` starts cannot deadlock because the
  request flag persists. An in-flight handler thread finishes on its own, since
  daemon threads are not joined.
- Registry. `TangoCdnIE`, `TangoFeedIE` and `TangoIE` claim disjoint URLs, and
  no built-in yt-dlp extractor competes (X7). `TangoBase` is not registered
  because yt-dlp keeps only classes whose names end in `IE`.
- `_NON_CONTENT_ROUTES` lowercases the id, so `NoScript.html` is caught, and it
  applies to the `/profile/` form as well.
- `UserNotLive` for profiles. Both `FAILED_TERMINATED` and a non-`LIVING`
  status become a wait, `--wait-for-video` re-resolves each cycle, and the
  check-in runs once because the visitor token is reused.
- `_id_from_body` rejects HTML error pages, so a gateway 404 reads as "no
  profile".
- Restore semantics. The restore only re-adds a key (domain, path, name) that is
  missing, so a rotated token from the reply is kept, and an anonymous run
  gains nothing (`test_an_anonymous_run_gains_no_account_cookies` bites).
- A `yt_dlp_plugins/__init__.py` in the plugin tree is harmless: yt-dlp loaded
  this plugin and a second one without `__init__.py` from the same
  `--plugin-dirs` container in either arrangement (X8).
- `original_url`, which would carry a copied token and username, is removed
  from `.info.json` unless `--no-clean-info-json` is set. Tags use
  `webpage_url`, which is query-free for the CDN path.
- Playlist rewriting leaves a master untouched if the CDN ever serves CRLF line
  endings, because the variant regex anchors on `$`. Today's playlists use LF.
- A playlist without `EXT-X-MEDIA-SEQUENCE` and with non-numeric segment names
  falls back to sequence 0 on every reload, so the window cannot merge. Real
  tango playlists carry the tag.
- Clock skew. `expireAt` is server time compared with the local clock. A local
  clock fast by more than the 8 s refresh interval would refresh on every
  request; a slow one would present expired tokens and fall into the 401 retry.
- `-v` prints the full copied URL including its token and username. Warn users
  not to paste verbose logs of a `tango:cdn` run.
- `watch` adds a viewer, so `-F`, `--simulate` and `--print` all count as views.
- The relay listens on loopback without authentication. Any local process can
  use it to fetch `cinema-*.tango.me` playlists with a valid token. It cannot
  read the token and a browser cannot read its responses cross-origin.
- yt-dlp archive ids differ per path: a profile or stream URL archives under
  the stream id, a copied CDN URL under the 32-hex path hash.

## Task list

Each group is one commit. Branch first, commit locally, run the gate, open a PR
into `main`, and do not merge it without the operator. Use the gate command:

```sh
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
  /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
```

| Group | Commit | Changes | Proof |
| --- | --- | --- | --- |
| T0 | Land the current work | Two commits from the uncommitted diff: (a) `TangoCdnIE`, `_relayed_formats`, their tests and docs; (b) the cookie snapshot and restore, its tests and docs. | Suite green on each commit alone. |
| T1 | `test: run the suite against the checkout` | F02. `tests/__init__.py` asserting the module path; the gate command in README, handoff and KB. | New `test_the_module_under_test_is_this_checkout`; mutants M1, M5, M12 fail when run from a copy (Appendix A). |
| T2 | `test: make the fake net enforce the contract` | F04, F05, F06, F31. Strict routes: token needs `Tango-VT` and `Referer`; watch needs `Accept: application/json`; playlists need the exact triplet; a `require_watch` switch. Cookie fidelity: expire `Tango-WST` and `Tango-ST` host-only, `Tango-RT` at two paths; precondition assert that removal happened. New tests: (1) CDN extractor makes a check-in and a token call and sends the triplet; (2) token route `[403, 200]` ends with a second check-in and a success; (3) expiring token triggers a refresh on `ensure()` after the margin; (4) `datadome` set by a token reply is gone after the next session step; (5) GET and HEAD against a live relay; (6) `no_proxy` set after `_relayed_formats`; (7) a media playlist fetched directly ends cleanly on 404; (8) non-dict check-in raises; (9) `http_headers` carry Origin and Referer; (10) CDN title has no query. | Each survivor in Appendix A (M2, M3, M4, M9, M10, M13, M15, M17, M19, M21, M23, M24, M26, M27, M28) now fails on its mutant; run the harness again and record the result here. |
| T3 | Live checks | Run L1 to L15 below in order. No code. Record dated results in the KB; set the fake's `require_watch` from L1. | Results table in the KB. |
| T4 | `fix: tango:cdn authorisation` (F01) | Branch on L1. Pass: keep the design, document the measurement. Fail: change the error and README, keep `TangoCdnIE` only if a supported route exists (for example an optional stream id), otherwise remove it. | The T2 CDN test, switched to the measured contract. |
| T5 | `fix: session and cookie handling` | F03 (per L2 and L3: restore after the token cycle, in a `finally`), F10 (`_watch` self-heal), F15 (JWT parse), F14 (no retry on 401 and 403), F18 (jar lock), F19 (drop `datadome` after tokens, if L10 says so). | Tests: token calls after a forced check-in carry no account cookies while the jar regains them afterwards; `watch` `[403, ok]` yields two check-ins and a recording; `_has_visitor_token` returns False for list payload and string `exp`; a 401 on `watch` makes one request; jar iteration raising `RuntimeError` once still succeeds. |
| T6 | `fix: relay lifecycle and failure behaviour` | F07 (`live` flag, no pruning for recordings, ffmpeg protocol), F08 (refresh cool-down), F09 (two-404 confirmation, only if L6 finds transient 404s), F13 (close an unused relay), F16 (monotonic clock), F17 (quiet `handle_error`), F32 (host check). | Tests: 100-segment ended playlist served whole; N fetches inside the cool-down make at most 3 token calls; reaper closes a relay with no ffmpeg request after the grace period (patched clock); disconnecting client leaves stderr empty; non-tango master host raises. |
| T7 | `fix: url and metadata edge cases` | F21 and F22 (lookahead and case-insensitivity), F20 (quote username, if L11 warrants), F12 (age limit rule from L9). | `test_urls.py`: `/Live` goes to the feed, `/Stream/<id>` is a stream, `www.../v2/<hex>/master.m3u8` is not an alias; an info test for the chosen `age_limit` rule. |
| T8 | `docs: correct claims` | F11, F19, F23 to F28 and F30's README row. Re-count tests after T1 to T7. | `git diff` touches docs and fixtures only; the suite is green; a grep for the F23 strings returns nothing. |
| T9 | `build: packaging` | F29. `[build-system]`, pip and pipx instructions, `ln` guard, install message, min versions from L14. | `pip wheel` from a clean copy; wheel listing equals `yt_dlp_plugins/` plus metadata; install into a venv and see three extractors in the yt-dlp registry. |
| T10 | `chore: chat recorder` | F30. Move to `contrib/` or remove. | README file table matches the tree. |

The audit ledger entry required by the workstation rules is deferred to the
implementation pass; this review was told to create one file only.

## Needs a live check

Work in a scratch directory and delete it afterwards, since it holds cookies:
`mkdir -p /tmp/tango-live-check && cd /tmp/tango-live-check`, then
`rm -rf /tmp/tango-live-check` when done. `<stream-url>` is a live stream or
profile URL, `<edge>` the CDN host, `<hash>` the path component under `/v2/`
seen in `yt-dlp -v` output, `<stream-id>` from `yt-dlp --simulate --print id`.

Shared header set for the raw probes (works in bash and zsh):

```sh
H=(-H 'Origin: https://www.tango.me' -H 'Referer: https://www.tango.me/' -H 'Accept: application/json')
```

**L1. Does a playlist need `watch`? (F01, decides T4)** Two runs on fresh
visitors, tokens used within seconds because they live ten:

```sh
for mode in nowatch watch; do
  J=jar-$mode.txt; rm -f $J
  curl -s -o /dev/null -c $J -b $J "${H[@]}" -H 'Content-Type: application/json' -d '{}' \
    'https://www.tango.me/api/visitor-lobby/api/public/visitors/v1/check-in?appId=tango'
  [ $mode = watch ] && curl -s -o /dev/null -c $J -b $J "${H[@]}" -X POST \
    -H 'Content-Type: application/json' \
    'https://www.tango.me/api/visitors/stream/client/v2/stream/<stream-id>/watch?requestId='
  T=$(curl -s -c $J -b $J "${H[@]}" 'https://www.tango.me/api/stream/token-data/v1/web/visitor')
  C="tt=$(jq -r .token <<<"$T"); ttu=$(jq -r .username <<<"$T"); tte=$(jq -r .expireAt <<<"$T")"
  curl -s -o /dev/null -w "$mode: %{http_code}\n" -H "Cookie: $C" \
    -H 'Origin: https://www.tango.me' -H 'Referer: https://www.tango.me/' \
    'https://<edge>.tango.me/v2/<hash>/master.m3u8'
done
```

Expect `watch: 200`. If `nowatch` is also 200, `tango:cdn` is sound. If it is
401, F01 stands. Then confirm end to end:
`timeout -s INT 40 yt-dlp -v -o 'cdn-%(id)s.%(ext)s' 'https://<edge>.tango.me/v2/<hash>/live.m3u8?token=x&username=x'`
and `ffprobe` the file.

**L2. Are restored account cookies still accepted? (F03, F26)** Needs a logged-in
Chrome profile.

```sh
yt-dlp --cookies-from-browser chrome --cookies after.txt --simulate '<stream-url>'
curl -s -o /dev/null -w '%{http_code}\n' -b after.txt "${H[@]}" \
  'https://www.tango.me/api/proxycador/api/public/v1/profiles/v2/me'
awk -F'\t' '$6 ~ /^Tango-/ {print $1, $6}' after.txt | sort
```

Expect `200` and the account cookie names present. The KB measured 401 after a
bare check-in; this shows the restore undoes it.

**L3. Does the 403 recovery still work with restored cookies? (F03)** A/B on
one account, a minute each, HEAD in a worktree and the working tree here:

```sh
timeout -s INT 60 yt-dlp -v -f 1 --cookies-from-browser chrome '<stream-url>' 2>&1 | tee run.log
grep -c 'Retrying stream token' run.log; grep -c 'Re-registering visitor' run.log
```

A rise in either count against HEAD, or a "Could not refresh stream token"
warning, means the restore broke recovery.

**L4. Followers-only stream (open item, F26).** Same command as L3 against a
followers-only broadcast the account follows. Record which of 401, 403 or a
`FAILED_*` code `watch` returns when logged out.

**L5. Stream ending mid-recording on the windowed build (README:225-228).**
`yt-dlp -v -f 1 '<profile-url>' 2>&1 | tee end.log` on a broadcast that is about
to stop; then `grep -n 'stream has ended' end.log` and `ffprobe` the file. Also
note whether any throttle replay occurred.

**L6. Transient 404 rate (F09).** While a stream is live and the L1 variables are
set, poll its media playlist once a second for 30 minutes and count non-200s:
`for i in $(seq 1800); do curl -s -o /dev/null -w '%{http_code}\n' -H "Cookie: $C" 'https://<edge>.tango.me/v2/<hash>/live.m3u8'; sleep 1; done | sort | uniq -c`
(refresh `C` as in L1 every eight seconds, or use the relay's own log with `-v`).
Any `404` before the broadcast ends justifies the two-404 confirmation.

**L7. Stale visitor token (F10).** Take a cookie file holding a `Tango-VT` whose
payload `exp` is in the future but whose signature is altered, then
`yt-dlp -v --cookies stale.txt --simulate '<stream-url>'`. Record whether
`watch` fails and with which status.

**L8. Format order (F11).** On five different live broadcasts:
`yt-dlp -F '<stream-url>'` and
`yt-dlp --simulate -S res --print '%(format_id)s %(resolution)s' '<stream-url>'`.
Confirm `-S res` always picks the larger rendition and `-f 1` does not.

**L9. `nsfw` against `age_limit` (F12).** `yt-dlp --write-pages --simulate '<stream-url>'`
on several broadcasts; read `details.nsfw` and `details.moderationLevel` in the
dumped `watch` body and compare.

**L10. Cookies left in the jar (F19).** `yt-dlp --cookies jar.txt --simulate '<stream-url>'`
then `awk -F'\t' '$6 ~ /^(tt|ttu|tte|datadome|_dd_s)$/ {print $6, $5}' jar.txt`.
Expect none, or only short-lived entries.

**L11. Username alphabet (F20).** Twenty fresh visitors, count unexpected
characters: run the check-in and token calls from L1 in a loop and
`jq -r .username | grep -c '[^A-Za-z0-9_-]'`. Expect `0`.

**L12. Does `www` serve `/v2/`? (F21)**
`curl -sI 'https://www.tango.me/v2/<hash>/master.m3u8' | head -1`. A 404 or an
HTML page supports adding the lookahead; a 401 or 200 means the CDN is behind
`www` and `TangoCdnIE` should accept it.

**L13. Meaning of `duration` (F27).** `yt-dlp --simulate --print timestamp '<stream-url>'`
twice, 60 s apart. The two values should agree within a few seconds and match
the broadcast's start as the site shows it.

**L14. Version floor (F28).**
`python3 -m venv /tmp/tv && /tmp/tv/bin/pip install 'yt-dlp==2024.03.10' && XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. /tmp/tv/bin/python -m unittest discover -s tests`,
then step the pinned version forward until the suite passes; that is the floor.

**L15. Chat recorder (F30).** `pip install websockets` and
`python3 contrib/tango_chat_recorder.py x --ws-url ws://127.0.0.1:9 -o /tmp/x.srt`.
A `TypeError` naming `extra_headers` confirms the keyword break.

**L16. Mixed-playlist session (cookies, several URLs in one run).** Two profile
URLs in one command with `--cookies-from-browser chrome`, the first forced
through a token 403 if possible, then confirm the second still reaches `watch`
as the account (L2's `me` probe on the saved cookie file after the run).

## Do not change

These look wrong and are deliberate.

- `TangoTokenRefresher.MARGIN = 2.0` (`tango.py:76`): a refresh every eight
  seconds, measured at about 14 refreshes in 115 s (KB:308).
- The token triplet travels in a `Cookie` request header, not the jar
  (`tango.py:421-426`). The docs wording needs fixing (F19); the mechanism does
  not.
- `ensure()` holds its lock across the network call (handoff:340-342). The old
  `_refreshing` flag was unreachable. Add a cool-down (F08); do not remove the
  lock.
- 404 is an answer, not a failure: `expected_status=404` on the alias and
  stream-id lookups (`tango.py:756-773`).
- The URL form decides profile versus stream. A bare `/<22-char-id>` is a
  profile (handoff:51-53, `tango.py:738-763`).
- The 404/410 check runs before the retry decision, and only for paths in
  `_served` (`tango.py:439-441`); an unserved path stays loud
  (`test_a_path_the_relay_never_served_still_raises`).
- Origin-form paths only in `fetch` (`tango.py:403-404`).
- Segments are rewritten to absolute CDN URLs so video bypasses Python
  (`tango.py:525-536`).
- `PLAYLIST_WINDOW = 45` stays under the measured 70 s segment retention
  (`tango.py:359`, KB:79-86). Do not raise it. F07 changes only recordings.
- Segment tags travel with their segment (`tango.py:134-174`). Hoisting them
  dropped playback to 0.04x (KB:358-361).
- `note=False`, not `None`, in `TangoTokenRefresher.refresh` (`tango.py:98-100`).
- `Request(..., proxies={'all': '__noproxy__'})` for the master fetch, and the
  `no_proxy` edit (`tango.py:878-880`, `319-336`). The process-wide environment
  change is accepted for the CLI (handoff:309-312).
- `_DROP_COOKIES` drops `datadome` and `_dd_s` at session start
  (`tango.py:589`).
- A profile URL turns an ended stream into `UserNotLive`; a stream URL keeps the
  `completeListUrl` fallback (`tango.py:939-947`).
- `_retry` re-wraps the exhausted error as unexpected (`tango.py:564-573`).
- Alias case is not normalised: every case variant resolves to the same account
  (handoff:325-327). F22 concerns reserved routes and `kind`, not aliases.
- `TangoBase` must keep a name that does not end in `IE`; as an IE subclass
  without `_VALID_URL`, `suitable()` raises `TypeError` (X7).
- `TangoCdnIE` excludes `www` and `m` hosts so one extractor owns each URL
  (`tango.py:1116-1120`). Revisit only per L12.
- The copied URL's query is dropped and `webpage_url` is the query-free path
  (`tango.py:1137-1155`); `test_a_copied_cdn_playlist...` pins it (M6 fails it).
- `time.time()` for the token expiry (`tango.py:106`): `expireAt` is server
  epoch time. Only the reaper moves to a monotonic clock (F16).
- `yt_dlp_plugins/__init__.py` and `extractor/__init__.py` stay; yt-dlp loads
  the plugin with or without them (X8).

## Suite result

Command, run once on the working tree as it stands (the symlink install resolves
to this repo, so the code under test is this checkout):

```sh
PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
```

Result: `Ran 63 tests in 15.926s`, `OK`. The log carries expected warnings from
tests that force retries (503 on `watch`, 401 on `watch`, 404 on the profile
page). Per F02, this result is valid only because the installed plugin is a
symlink to this checkout.

## Appendix A: mutation results

Each row is one textual change to `tango.py` in a temp copy, with the suite run
as `XDG_CONFIG_HOME=<empty> PYTHONPATH=. python -m unittest discover -s tests`.
Before that fix every mutant passed, because the installed copy was imported.

| Mutant | Change | Result |
| --- | --- | --- |
| M1 | `_restore_account_cookies` does nothing | killed by `test_the_account_session_survives_the_check_in` |
| M2 | snapshot only `Tango-RT` | survived |
| M3 | `TangoCdnIE` skips `_ensure_session` | survived |
| M4 | `TangoCdnIE` passes `live=False` | survived |
| M5 | relay sends no `Cookie` header | killed by `test_the_upstream_request_carries_a_timeout_and_the_token` |
| M6 | `TangoCdnIE` keeps the copied query | killed by the CDN extraction test |
| M7 | `age_limit` 0 | survived |
| M9 | forced session does not reset the visitor | survived |
| M10 | `_issue_token` never forces a check-in | survived |
| M12 | 404 and 410 never end a recording | killed by two relay tests |
| M13 | `_drop_bot_cookies` does nothing | survived |
| M14 | profile URLs treated as stream URLs | killed by five tests |
| M15 | `ensure()` never refreshes unless forced | survived |
| M16 | timestamp sign flipped | killed by `test_a_live_stream_carries_its_start_time` |
| M17 | `TangoCdnIE` skips the token call | survived |
| M18 | window never prunes | killed by two window tests |
| M19 | `fetch` never records `_served` | survived |
| M20 | stale replay disabled | killed by `test_replays_last_playlist_when_the_token_is_throttled` |
| M21 | snapshot every `Tango-SD` | survived |
| M22 | JWT `exp` ignored | killed by `test_expired_token_needs_a_check_in` |
| M23 | non-dict check-in reply accepted | survived |
| M24 | `_relayed_formats` skips the proxy exemption | survived |
| M26 | `TangoCdnIE` puts the full URL in the title | survived |
| M27 | formats lose their Origin and Referer headers | survived |
| M28 | feed extractor skips the session | survived |
| M29 | wait hint always shown | killed by `test_the_wait_hint_is_not_repeated_when_already_waiting` |
| M30 | `_end_playlist` does nothing | killed by three tests |

Survivors: 16 (M2, M3, M4, M7, M9, M10, M13, M15, M17, M19, M21, M23, M24, M26,
M27, M28). Killed: 11 of the 27 mutants run. M7 waits on the F12 decision (T7); the
others are covered by T2.

## Appendix B: experiments

- X1. Which copy is imported: `import tests.test_relay`, then
  `inspect.getsourcefile(TangoHlsRelay)`, with and without
  `XDG_CONFIG_HOME` pointing at an empty directory.
- X2. `_MediaWindow(45).merge()` on a 300-segment playlist ending in
  `#EXT-X-ENDLIST`: 45 segments out.
- X3. `relay.fetch()` with the token route answering 403 and an expired token:
  3 token calls, 1 forced check-in, sleeps of 1 s and 2 s per failing refresh.
- X4. `close()` twice, then thread listing: only `MainThread` remains; the
  reaper compares `time.time()` values.
- X5. `_has_visitor_token` on a list payload (`AttributeError`), a string `exp`
  (`ValueError`) and an object `exp` (returns True).
- X6. A client that resets its connection after sending a request: stderr gets a
  `BrokenPipeError` traceback.
- X7. Live extractor registry: the three Tango classes are listed, `TangoBase`
  is not, and the URL probes give `tango:cdn` for a `cinema-*` host, `tango`
  for the `www` and apex `/v2/<hex>/master.m3u8` forms, `/Live` and `/LIVE/`,
  and `tango` for any first path segment such as `/api/x`. `TangoBase.suitable`
  raises `TypeError`.
- X8. Wheel build from a copy (contents above) and a `--plugin-dirs` load test
  with a second plugin, with and without the `__init__.py` files; both
  arrangements list all extractors.

## Outcome

Recorded 2026-11-21 after groups 1 to 5 landed in the working tree
(uncommitted on `fix/cdn-url-and-account-session`). Gate: 93 tests, OK;
101 after the review fixes below.
Test names are in `tests/`.

- F01 rejected. Measured 2026-11-21: a fresh `token-data` triplet gets 200
  for master and rendition on an empty jar with no `watch` and no stream
  page visit; no cookie and a tampered `tt` both get 401. The old 401 came
  from the stale static token. `TangoCdnIE` keeps skipping `watch`; only the
  KB sentence and the `_watch` docstring changed.
  (`test_a_copied_cdn_url_is_authorised_by_the_triplet_alone`)
- F02 landed. `test_the_module_under_test_is_this_checkout`; gate command
  with `XDG_CONFIG_HOME` in README, KB and handoff.
- F03 landed, then revised on review. The restore after the check-in stays;
  the token calls keep the account cookies. Taking them out for each token
  cycle ran on a daemon relay thread, so a Ctrl-C could save a `--cookies`
  file without them. Live confirmation of the 403 recovery with the account
  cookies present is pending (L3). `test_token_calls_carry_the_account_cookies`,
  `test_the_jar_never_lacks_the_account_during_an_extraction`.
- F04 landed. Strict fake (`FakeRH._refusal`).
  `test_the_fake_cdn_answers_like_the_measured_one`,
  `test_a_copied_cdn_url_is_authorised_by_the_triplet_alone`.
- F05 landed. `test_a_throttled_token_recovers_with_a_fresh_visitor`,
  `test_a_token_inside_the_margin_is_refreshed_once`,
  `test_datadome_from_a_token_reply_rides_on_the_next_token_call`.
- F06 landed. `test_without_the_restore_the_check_in_logs_the_account_out`,
  `test_the_account_session_survives_the_check_in`.
- F07 landed. An ended playlist is served whole:
  `test_an_ended_playlist_is_served_whole_and_a_live_one_is_windowed`. A
  live playlist that ends keeps its merged window:
  `test_a_live_playlist_that_ends_keeps_its_window`. The proxy half applies
  after all: the parser emits `m3u8_native`, not `m3u8`, so a recording went
  to `HlsFD` and through `--proxy`. Relayed formats are now `protocol: m3u8`
  (ffmpeg); an 18 s recording checked by hand used ffmpeg.
  `test_relayed_formats_are_downloaded_by_ffmpeg`,
  `test_the_proxy_rule_behind_the_ffmpeg_protocol`.
- F08 landed. `test_a_failing_refresh_is_not_repeated_per_playlist_reload`,
  `test_a_failed_refresh_cools_down_while_the_token_still_works`,
  `test_an_expired_token_gets_no_cool_down`.
- F09 deferred. Measured: 507 one-second polls of a live media playlist over
  10 minutes, all 200, so no transient 404 was seen and the single-404 rule
  stays.
- F10 landed. `test_a_401_with_a_jar_visitor_re_registers_once`,
  `test_an_issued_visitor_is_not_re_registered_on_a_401`. On review,
  `_checked_in` is reset when a run reuses a jar visitor, so a second run on
  the same extractor (`--wait-for-video`) can still self-heal:
  `test_a_second_run_on_the_same_instance_can_self_heal`.
- F11 landed. README, KB, handoff and `install.sh` use `-S res`; a sampled
  master listed 1280x720 first and 640x360 second.
- F12 landed (documented, keep 18). Six catalog items sampled 2026-11-21 all
  had `isNsfw` false and `moderationLevel` 2; recorded next to the code, in
  the KB and in the README.
- F13 deferred. The relay lingers after `-J`, `-F` or `--simulate` until the
  900 s idle timeout; its own issue.
- F14 landed. `test_a_refused_watch_is_not_retried_beyond_the_re_registration`.
- F15 landed. `test_an_unreadable_token_needs_a_check_in`,
  `test_an_unreadable_jar_visitor_is_replaced`.
- F16 landed. `time.monotonic()` for `_last_request` and the reaper.
  `test_the_idle_reaper_follows_the_monotonic_clock`.
- F17 landed. `test_a_client_hanging_up_mid_response_is_quiet`.
- F18 landed. `test_a_jar_changing_mid_iteration_is_read_again`; on review
  `_drop_cookies` also clears under the jar lock:
  `test_dropping_cookies_holds_the_jar_lock`.
- F19 landed. Wording corrected in the KB, PLAN, handoff and the
  `TangoTokenRefresher` docstring: the relay sends its own `Cookie` header,
  `token-data` does set `tt`/`ttu`/`tte` and `datadome`, and
  `_drop_bot_cookies` drops `datadome` and `_dd_s` at each session step. The
  remaining gap is pinned, not fixed:
  `test_datadome_from_a_token_reply_rides_on_the_next_token_call`.
- F20 landed. `test_the_token_username_is_quoted_in_the_master_url`.
- F21 landed. Measured: `www` answers `/v2/<hash>/master.m3u8` with 200
  `text/html`, `m` did not answer. `test_a_cdn_path_on_the_site_host_matches_nothing`.
- F22 landed. `test_route_names_ignore_case`,
  `test_a_capitalised_stream_route_is_a_stream`.
- F23 landed. Aliases, ids, display name and follower count replaced with
  the `tests/fake_net.py` values or `<alias>` / `<id>` placeholders in the
  KB, README, PLAN, the #11433 plan, `_TESTS` and `tests/test_urls.py`; a
  grep for the replaced strings returns nothing. Git history still has them.
- F24 landed. Registry (three extractors), test count, architecture block
  and file lists updated in KB, PLAN, README and handoff.
- F25 landed. "Stalls until the token returns" in README, PLAN and the
  `STALE_PLAYLIST_MAX_AGE` comment.
- F26 landed. `[needs live check]` on the restore in README, KB and handoff.
- F27 landed (evidence only). Two `--simulate --print timestamp` runs 60 s
  apart gave `<epoch>` and `<epoch>` minus one, so `duration` is elapsed broadcast
  time and `timestamp` is the start. No code change. yt-dlp's `duration` is
  `NA` for a live stream.
- F28 landed. README names the tested version (2026.10.26);
  `pyproject.toml` states the tested floor in a comment and sets
  `requires-python >=3.11`, the floor of the tested yt-dlp. Older versions
  remain unmeasured (L14).
- F29 landed. `[build-system]` added; the wheel from a clean copy holds only
  `yt_dlp_plugins/` plus metadata. README lists the three installs and warns
  about pip into another environment. `install.sh` refuses a real directory
  at the link path and says its check makes two requests.
- F30 landed. Moved to `contrib/tango_chat_recorder.py` with a docstring
  saying it targets the retired gateway and is unused; `extra_headers` left
  as is and noted.
- F31 landed. `test_get_and_head_over_a_socket`,
  `test_extraction_exempts_loopback_from_the_proxy`,
  `test_a_media_playlist_fetched_directly_ends_cleanly_on_404`.
- F32 landed. `test_only_tango_hosts_are_relayed`.
