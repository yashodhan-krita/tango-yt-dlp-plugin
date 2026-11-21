# Handoff — tango.me yt-dlp plugin

State as of 2026-11-21, updated after the adversarial-hardening round.
`main` is at `90d7f52` (PR #1, the no-JavaScript page error). Branch
`fix/cdn-url-and-account-session` holds everything since as a release
candidate; commits land next:
`TangoCdnIE`, the shared `_relayed_formats`, the account cookie
snapshot/restore, the hardening fixes and tests, these document updates, the
packaging changes and the move of the chat recorder to `contrib/`. 102 offline
tests pass with the gate command in section 7. The plan and its outcome are
in `docs/superpowers/plans/2026-11-21-adversarial-hardening.md`. Chrome was
signed out at the time of writing, so nothing that needs a signed-in browser
was re-measured.
This document is for whoever picks the plugin up next. It records what the
tool does now, the contracts the site imposes, the parts that are easy to
break, and the work that is still open. The short version of the site's
behaviour lives in `docs/kb/tango-me.md`; this file is the project handoff.

## 1. What this is

A yt-dlp extractor plugin for live streams on tango.me. It records a live
broadcast through a loopback HLS relay, and it tracks a broadcaster by
profile URL so the same command works across separate broadcasts.

- Entry point: `yt_dlp_plugins/extractor/tango.py`. Three extractors are
  registered: `tango:live` (`TangoFeedIE`, the `/live` catalogue), `tango`
  (`TangoIE`, stream and profile URLs) and `tango:cdn` (`TangoCdnIE`, a CDN
  playlist URL copied from the browser). `TangoBase` holds the shared code
  and is not registered (its name does not end in `IE`).
- Installed as a plugin at
  `~/.config/yt-dlp/plugins/tango-yt-dlp/yt_dlp_plugins`, symlinked to this
  repo. Edits are live in the next `yt-dlp` run; no reinstall needed.
- Public repo: <https://github.com/yashodhan-krita/tango-yt-dlp-plugin>.
  Licence: Unlicense, the same as yt-dlp; see `LICENSE`.
- No yt-dlp core code is patched. Keep it that way unless a core bug forces
  it, and record why if it ever happens.

## 2. User-facing contract

| URL form | Meaning |
| --- | --- |
| `/stream/<id>`, `/live/<id>` | a stream id (a non-22-char value is still accepted as a legacy alias) |
| `/<alias>`, `/@<alias>`, `/profile/<alias>` | a profile |
| `/profile/<account-id>` | a profile, addressed by the site's 22-char account id |
| `/live`, `/live/` | the live catalogue (feed list extractor) |

Rules that users can rely on:

- A profile URL always means *the current live stream*. If the broadcaster is
  offline, the run raises `UserNotLive`, and `--wait-for-video N` re-resolves
  the profile every N seconds.
- If a broadcast ends between resolution and the `watch` call, a profile URL
  raises `UserNotLive` again (both `FAILED_TERMINATED` and a non-`LIVING`
  status), so waiting continues instead of downloading the previous recording.
- A `/stream/<id>` URL keeps the archival behaviour: it falls back to the
  stream's finished recording when the stream is no longer live.
- Profile metadata (display name, avatar, bio, follower count, account id)
  comes from the public profile page, so it is available even when the
  broadcaster is offline.
- A bare `/<22-char-id>` is a profile now, not a stream. That is a deliberate
  behaviour change from the first version; use `/stream/<id>` to mean a
  stream id.
- `/stream/noscript.html`, `/live/noscript.html` and `/noscript.html` are
  tango's no-JavaScript page. They raise an expected error naming that page
  and asking for the original stream or profile URL, with no network request
  (`_NON_CONTENT_ROUTES`).

Recommended tracking command (README has the long form):

```sh
while :; do
    yt-dlp --wait-for-video 60 --download-archive ~/.cache/tango-archive.txt \
        -S res "https://www.tango.me/@<alias>" || true
    sleep 30
done
```

## 3. Resolution chain

```
profile identifier (alias or account id)
  └─ GET /api/visitors/profilealias/api/alias/convert?alias=<identifier>
       ├─ 200 + 22-char body  -> account id
       └─ 404                  -> use the identifier as the account id
                                  (only valid on a profile URL)
  └─ GET /api/showroom-streams/api/public/stream/v1/id?broadcasterId=<account id>
       ├─ 200 + 22-char body  -> current live stream id
       └─ 404                  -> UserNotLive (offline)
  └─ POST /api/visitors/stream/client/v2/stream/<stream id>/watch
       ├─ code OK + details.stream -> stream document (metadata, masterUrl)
       ├─ code FAILED_TERMINATED   -> UserNotLive on a profile URL
       └─ other codes              -> WATCH_ERRORS message, expected error
```

Every lookup goes through `RetryManager`; only semantic 404s use
`expected_status=404` and stay single-request answers. Response bodies are
accepted as ids only when they match the 22-char shape
(`_id_from_body`), so a gateway or CDN error page reads as "no profile" /
"not live" rather than travelling on as an id.

### Why the URL form decides

Stream ids and account ids are both 22-char base64url values. The string
shape cannot tell them apart, and a profile page accepts either value. The
only reliable discriminator is where the value appeared in the URL, which is
why `kind` is captured in `_VALID_URL` and the extractor never guesses.

## 4. Architecture

```
TangoBase (InfoExtractor)
├── _ensure_session      visitor check-in; drops datadome/_dd_s each step
├── _check_in            snapshots account cookies, restores them after
├── _issue_token         token calls; jar untouched, 403 -> new visitor
├── _resolve_stream_id   alias -> account id -> stream id  (returns both ids)
├── _watch               registers the viewer, returns the stream document
├── _profile_metadata    schema.org ProfilePage -> uploader fields
├── _hls_formats         asks the relay for the master playlist
└── _relayed_formats     builds the relay, exempts loopback, reads formats
TangoFeedIE  (tango:live)  /live catalogue
TangoIE      (tango)       stream and profile URLs -> _extract_stream
TangoCdnIE   (tango:cdn)   copied /v2/<hash>/*.m3u8 -> check-in, token, relay

TangoTokenRefresher      re-issues tt/ttu/tte every ~8 s, cool-down on failure
TangoHlsRelay (127.0.0.1, random port; tango.me hosts only)
├── _TangoRelayHandler   HTTP/1.0, 30 s handler timeout
├── _MediaWindow         merges recent live playlists into a window
└── fetch                upstream through yt-dlp's urlopen stack
```

`TangoIE` and `TangoCdnIE` both reach the relay through
`_relayed_formats`, so the proxy exemption and the close-on-failure rule
live in one place. `TangoCdnIE` makes no `watch` call: a fresh triplet
authorises a playlist on its own (measured 2026-11-21).

The relay exists because ffmpeg cannot read yt-dlp's cookie jar and the CDN
token lives about ten seconds. Each playlist request fetches upstream with a
fresh triplet in a plain `Cookie` header. The relay does not store the
triplet in the jar; the `token-data` reply sets it there itself for ten
seconds (see the KB, Architecture). The relay also merges playlist
reloads into a window. It never polls; it merges only when ffmpeg asks, so
the window covers ffmpeg running up to about 45 s behind the live edge, but
a gap of more than the CDN's ~8 s window between two reloads still loses the
segments in between. A 404/410 for a playlist it already served ends the
recording cleanly with `#EXT-X-ENDLIST`.

The visitor check-in logs a `--cookies-from-browser` account out: its reply
expires the host-only `Tango-WST`, `Tango-SD` and both `Tango-RT` paths; it
does not expire `Tango-ST`
(measured 2026-11-21: `me` answers 200 before the check-in and 401 after it;
`test_without_the_restore_the_check_in_logs_the_account_out`).
`_check_in` snapshots `Tango-ST`, `Tango-RT`, `Tango-WST` and the account
`Tango-SD` and `_restore_account_cookies` re-adds whichever the reply
removed, in a `finally`, so `watch` runs with the account cookies in the jar.
The token request itself leaves the jar alone and carries them. A token 403
on the relay thread forces a check-in there too, which expires the account
cookies for the length of that one request before the restore; a Ctrl-C in
that window could save a `--cookies` file without them `[needs live check]`
(`test_the_jar_never_lacks_the_account_during_an_extraction`). That the site accepts the restored
cookies, and whether they change the 403 recovery on a token call, is not
measured `[needs live check]`. Details in `docs/kb/tango-me.md` under "Session rules".

`127.0.0.1` and `localhost` are appended to `no_proxy`/`NO_PROXY` once, so
ffmpeg does not tunnel the loopback relay. This mutates the process
environment by design; the CLI is the only supported embedding today.

## 5. Invariants worth protecting

1. **Never infer identity from the 22-char shape.** URL form only.
2. **A 404 from a lookup is an answer, not a failure.** It must not retry or
   count against the retry budget.
3. **Resolve before building the relay.** An offline profile must not leave a
   server and two threads behind on each wait cycle.
4. **A profile URL means now.** Ended and terminated streams become
   `UserNotLive`; only `/stream/<id>` may hand back a finished recording.
5. **Metadata never blocks a recording.** A missing or malformed page warns
   and the recording goes on. For an alias URL only the profile page is
   fetched, and a failure leaves the alias as the name
   (`test_a_missing_alias_profile_page_keeps_the_alias_as_the_name`). For a
   stream URL only the stream page is fetched, and a failure leaves the
   stream id as the name
   (`test_a_missing_profile_page_does_not_stop_the_recording`).
6. **The token travels in the relay's `Cookie` header, never in a log
   line.** The username does not stay private: `_extract_stream` appends
   `&username=<ttu>` to the master URL, the relay keeps that query on its
   loopback URL, and yt-dlp reports it as `manifest_url` in `-j` and
   `--write-info-json`. Whether that username is
   account-bound on a logged-in run is `[needs live check]`. (The token
   reply's own `Set-Cookie` puts the triplet in the jar for ten seconds;
   that is the site's doing.)
7. **Relay paths are origin-form.** `@host/x`, `//host/x` and absolute URLs
   are rejected with 400 so the cookie cannot be sent elsewhere.
8. **Relay state is shared between handler threads.** `_cache`, `_served`
   and `_MediaWindow` mutations happen under one `RLock`.

## 6. Layout

| Path | What it is |
| --- | --- |
| `yt_dlp_plugins/extractor/tango.py` | the whole plugin: session, resolver, relay, extractors |
| `docs/kb/tango-me.md` | site contract and the *why* behind each rule. Read this first. |
| `PLAN.md` | original reverse-engineering notes; historical, not a contract |
| `README.md` | user usage, tracking loop, profile forms |
| `tests/fake_net.py` | fake yt-dlp transport, routes by method/host/path |
| `tests/fixtures/` | scrubbed fixtures; provenance table in its README |
| `tests/test_environment.py` | fails if the suite imported an installed copy instead of the checkout |
| `tests/test_extract.py` | end-to-end extraction against the fake transport |
| `tests/test_urls.py` | URL forms, reserved routes, profile/stream split |
| `tests/test_relay.py` | playlist rewrite, window, upstream, ended stream, paths |
| `tests/test_session.py` | visitor token, refresh and cool-down behaviour |
| `install.sh` | symlink installer; refuses a real directory at the link path |
| `pyproject.toml` | wheel metadata; the wheel holds only `yt_dlp_plugins/` |
| `contrib/tango_chat_recorder.py` | unused chat sketch for the retired gateway; not part of the plugin |
| `docs/superpowers/` | earlier design and plan for the recording/hardening round |

`profile.html` in the fixtures is a **stream** page; the name predates
profile tracking. The profile pages are `profile_page.html` (alias) and
`profile_page_account.html` (account id). Both mirror the live three-block
JSON-LD (Organization, WebSite, ProfilePage) but are scrubbed
reconstructions, not literal captures.

## 7. Commands

Run the suite with the interpreter that ships with yt-dlp, not the system
Python, or `yt_dlp` will not import. This is the gate:

```sh
cd ~/Projects/tango-yt-dlp-plugin
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
  /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
```

`XDG_CONFIG_HOME` keeps yt-dlp's plugin finder from importing the installed
copy under `~/.config/yt-dlp/plugins/` ahead of `PYTHONPATH`; without it
the suite tests that copy, and `test_the_module_under_test_is_this_checkout`
fails. 102 tests on 2026-11-21.

The Homebrew path is version-pinned. After `brew upgrade yt-dlp`, refresh it
with `$(brew --prefix yt-dlp)/libexec/bin/python` or the tests fail on import.

Live checks that cost little:

```sh
# resolve and print metadata only
yt-dlp --simulate --no-warnings --print \
    '%(id)s|%(uploader)s|%(uploader_id)s|%(channel_follower_count)s' \
    'https://www.tango.me/@<alias>'

# profile addressed by account id
yt-dlp --simulate --no-warnings --print '%(id)s %(channel_id)s' \
    'https://www.tango.me/profile/<account-id>'

# offline profile: expect "is not live; retry with --wait-for-video 60"
yt-dlp --simulate --no-warnings 'https://www.tango.me/@<offline-alias>'
```

Recording proof (stop with Ctrl-C; yt-dlp finalises and MP4-fixes):

```sh
mkdir -p /tmp/tango-check && cd /tmp/tango-check
timeout -s INT 30 yt-dlp -S res --newline 'https://www.tango.me/@<alias>'
ffprobe -v error -show_entries format=duration:stream=codec_name,width,height \
    -of default=noprint_wrappers=1 ./*.mp4
```

A healthy run is 720x1280 (or similar), H.264 + AAC, and no `skipping`
lines in the log.

Debug switches that matter: `-v` for the extractor's own debug lines (for
a copied CDN link it prints the URL with its `token=` and `username=`, so do
not paste that log),
`--write-pages` to keep the HTML the resolver saw, `--no-warnings` when a
`--print` check should stay quiet.

## 8. Commit history of the last round

`main` after the profile work (handoff commit not listed):

```
cc54b97 docs: cover account-id metadata and bare-id URLs
e0ee176 fix: harden profile resolution and the HLS relay
a07282c docs: document profile tracking and the account-id profile form
3768601 feat: resolve profile URLs by form and read profile metadata
a0ba6e6 docs: record the playlist window, retention and live proof
2ca14e6 fix: serve a merged playlist window instead of the CDN's 8 segments
```

There is a public remote now: `origin` is
`https://github.com/yashodhan-krita/tango-yt-dlp-plugin.git`, and `main`
tracks `origin/main`. Commits up to `cc54b97` were authored under the
`yashodhank` identity; the repo-local identity was switched to
`yashodhan-krita` for the documentation and licence commits. Future work
follows the stacked-PR rules in `~/.agent-workflow-rules.md`: branch, commit,
push, PR, merge when green.

## 9. Review status

An independent Claude Code review (read-only, plan mode) produced 14
findings. The full text is at
`/private/var/folders/y1/6v8l47y11hl50zw12yy18z5m0000gn/T/opencode/tango-review.md`
(a temp path; treat it as ephemeral). Applied in `e0ee176`:

| # | Finding | Status |
| --- | --- | --- |
| 1 | percent-encoded identifiers | fixed: one `unquote`, tested |
| 2 | 404 bodies trusted as ids | fixed: `_id_from_body` shape check |
| 3 | ended/terminated profile downloads a recording | fixed: `UserNotLive` for profile URLs |
| 4 | URL-form regex clarity, misleading bad-id error | fixed: `kind` group, reserved-route terminator, README note |
| 5 | reserved routes only excluded when bare | fixed: `(?:[/?#]|$)` |
| 6 | hand-rolled JSON-LD parsing | fixed: `_yield_json_ld`, `@graph`, list `mainEntity`, `str` checks, real 0 count, quoted alias |
| 7 | relay forwards any path | fixed: origin-form only, handler timeout |
| 8 | relay state unguarded | fixed: one `RLock`, mutation points wrapped |
| 9 | relay lifecycle leaks | partly fixed: proxy exemption inside `try`, stoppable reaper. Process-wide `no_proxy` edit is accepted by design |
| 10 | failed token refresh surfaces obscurely | fixed: explicit error when no token was issued |
| 11 | repeated wait hint | fixed: `_not_live` helper |
| 12 | mistyped 22-char alias waits forever | fixed: message says "treated as an account id" |
| 13 | account-id path drops metadata | fixed: account page read, `channel_id` fallback, `display_id` |
| 14 | test and fixture gaps | fixed: 51 → 59 tests, fixtures documented and shaped from the live page |

Verification after the fixes: 59 offline tests OK; live resolve for alias,
`@alias`, `%40alias` and account-id forms; account-id URL returns the
profile's follower count; offline profile raises the wait hint; 23–38 s recordings with 0
skipped segments.

### yt-dlp issue #11433 review (2026-11-21)

Every comment on <https://github.com/yt-dlp/yt-dlp/issues/11433> was read and
its claims probed live. One defect came out of it: `/stream/noscript.html`
was read as an alias and reported `No tango.me profile found for
"noscript.html"`. It now raises a named, expected error before any request
(`test_the_noscript_page_is_named_without_a_request`; 59 → 60 tests). The
rest is recorded in `docs/kb/tango-me.md` under "yt-dlp issue #11433": the
CDN path hash is not the stream id, `?token=` alone answers 401, `token-data`
sets the cookie triplet with `Max-Age=10`, and `watch` answers protobuf
without `Accept: application/json`.

## 10. Open work

Ordered by value, with the exact next step.

0. **Commit and open the PR.** `fix/cdn-url-and-account-session` is a
   release candidate; commits land next. Split it per the task list in the
   adversarial-hardening plan, run the gate on each commit, push, and open a
   PR into `main`.

1. **Followers-only / private streams are unproven.**
   `_watch` maps 401/403 to a message naming `--cookies-from-browser chrome`,
   but no `isPrivateStream: true` entry has been observed live, and the
   cookie restore has not run against the real site (Chrome was signed out
   when this was written). Next, with a signed-in Chrome:
   `yt-dlp --cookies-from-browser chrome --cookies after.txt --simulate
   '<stream-url>'`, then expect 200 from `profiles/v2/me` with `after.txt`
   (L2 in the plan). Then find a followers-only broadcast, note whether
   `watch` answers 401, 403 or a `FAILED_*` code, and add a fixture for
   whichever shape appears. `[needs live check]`

2. **A stream ending mid-recording was not re-observed on the windowed build.**
   The relay-side behaviour is unit-tested
   (`test_a_served_media_playlist_that_404s_ends_cleanly`,
   `test_ends_the_recording_when_the_token_stays_gone`), but the live case
   was last seen before the playlist window landed.
   Next: start a recording on a short broadcast, let it end, confirm the
   file finalises with the "stream has ended" warning and no error.
   A single 404 still ends a recording: a 10-minute poll on 2026-11-21 made
   507 one-second playlist requests, all 200.

3. **No CI yet.** Tests run by hand. The repo is public; a short workflow
   (install yt-dlp, run the gate command from section 7, including
   `XDG_CONFIG_HOME`) would make the suite a gate instead of a habit.

4. **Deferred from the adversarial plan.** F09 (two-404 confirmation): not
   built, because the 507-poll measurement saw no transient 404; revisit if
   one is ever seen. F13 (the relay lingers up to 900 s after a successful
   `-J`, `-F` or `--simulate`): its own issue. F27 needed evidence only and
   got it (two `--print timestamp` runs 60 s apart agreed within a second).

5. **Fixtures are scrubbed reconstructions.** They follow the live shapes but
   were not captured byte-for-byte. Next: capture
   `https://www.tango.me/<alias>` and `https://www.tango.me/<account-id>`,
   scrub ids/names/counters per `tests/fixtures/README.md`, replace the two
   profile page fixtures. Keep the three-block JSON-LD layout.

6. **`no_proxy` is process-wide.** Fine for the CLI, wrong for embedded use.
   If the plugin is ever imported into a long-lived process, save the old
   value in `_exempt_loopback_from_proxy` and restore it in
   `TangoHlsRelay.close`.

7. **yt-dlp issue #11433 is still open upstream.** Whether to post there is
   an operator decision, not a code change. A copied CDN playlist URL
   (`master.m3u8?token=`, `live.m3u8?token=` or any `*.m3u8` under
   `/v2/<hash>/`) is accepted by `TangoCdnIE`: the query is dropped and the
   path is relayed with a fresh token. No stream id and no `watch` are
   needed; measured 2026-11-21, a fresh triplet alone gets 200 from master
   and rendition.

8. **Version floor.** Only yt-dlp 2026.10.26 on CPython 3.15 has run the
   suite. Next: L14 in the plan (step pinned yt-dlp versions in a venv).

Settled since the last handoff: alias case does not matter. Every case
variant of an alias resolves to the same account id (measured 2026-11-21),
so the extractor does not normalise case. Route names (`/Live`,
`/Stream/<id>`) are now matched case-insensitively.

## 11. Known traps

- `_VALID_URL` is anchored on the first path segment. Anything added to the
  reserved-route list must keep the `(?:[/?#]|$)` terminator, or
  `/settings/privacy` becomes an alias.
- `_profile_metadata` must not raise on a bad page. It is a metadata source,
  not a gate.
- `_watch` codes are strings from the site. A non-string `code` has its own
  test; do not format it into a message without checking.
- The relay's `_served` set is what distinguishes "the stream ended" from
  "ffmpeg asked for a path that never existed". Do not clear it on a retry.
- `TangoTokenRefresher.ensure()` holds its lock across the network call.
  Re-adding a `_refreshing` flag is unnecessary now; the old flag was
  unreachable behind the non-reentrant lock.
- The Homebrew interpreter path in this document will go stale. Use
  `$(brew --prefix yt-dlp)/libexec/bin/python` in scripts.

## 12. Where the durable notes live

- Site contract and reasoning: `docs/kb/tango-me.md`.
- Fixture provenance and scrub rules: `tests/fixtures/README.md`.
- Audit ledger for this round:
  `~/.ai-audit/ledger/2026/20261120-182134Z-tango-me-profile-tracking-account-id-resolution-review-fixes.md`.
- Prior recording/hardening round:
  `~/.ai-audit/ledger/2026/20261120-062445Z-harden-tango-me-yt-dlp-plugin-offline-harness-retries-clean-ending-timestamp-pro.md`
  and `docs/superpowers/`.
