# Handoff — tango.me yt-dlp plugin

State as of 2026-11-21. The work described here ends at `cc54b97`; this
handoff is the commit after it. Working tree clean, 59 offline tests passing.
A later review of yt-dlp issue #11433 on branch
`fix/noscript-page-and-issue-notes` added a named error for tango's
no-JavaScript page; 60 tests pass there (see section 9).
This document is for whoever picks the plugin up next. It records what the
tool does now, the contracts the site imposes, the parts that are easy to
break, and the work that is still open. The short version of the site's
behaviour lives in `docs/kb/tango-me.md`; this file is the project handoff.

## 1. What this is

A yt-dlp extractor plugin for live streams on tango.me. It records a live
broadcast through a loopback HLS relay, and it tracks a broadcaster by
profile URL so the same command works across separate broadcasts.

- Entry point: `yt_dlp_plugins/extractor/tango.py` (`TangoIE`, plus
  `TangoBase` and the list extractor).
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
        -f 1 "https://www.tango.me/@<alias>" || true
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
       ├─ code OK + details.stream -> stream document, CDN authorised
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
├── _ensure_session      visitor check-in, account cookies preserved
├── _resolve_stream_id   alias -> account id -> stream id  (returns both ids)
├── _watch               registers the viewer; authorises the CDN
├── _profile_metadata   schema.org ProfilePage -> uploader fields
├── _hls_formats        asks the relay for the master playlist
└── TangoTokenRefresher  re-issues tt/ttu/tte every ~8 s

TangoHlsRelay (127.0.0.1, random port)
├── _TangoRelayHandler   HTTP/1.0, 30 s handler timeout
├── _MediaWindow         merges recent playlists into a window
└── fetch                upstream through yt-dlp's urlopen stack
```

The relay exists because ffmpeg cannot read yt-dlp's cookie jar and the CDN
token lives about ten seconds. Each playlist request fetches upstream with a
fresh triplet in a plain `Cookie` header. The token is never written into the
jar (it would leak onto unrelated requests). The relay also merges playlist
reloads into a window, so a late reload does not lose segments it would have
dropped; a 404/410 for a playlist it already served ends the recording
cleanly with `#EXT-X-ENDLIST`.

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
5. **Metadata never blocks a recording.** A missing or malformed profile page
   warns and falls back to the stream title (`test_a_missing_profile_page_
   does_not_stop_the_recording`).
6. **The token header is the only place the triplet goes.** Not the jar, not
   a URL, not a log line.
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
| `tests/test_extract.py` | end-to-end extraction against the fake transport |
| `tests/test_urls.py` | URL forms, reserved routes, profile/stream split |
| `tests/test_relay.py` | playlist rewrite, window, upstream, ended stream, paths |
| `tests/test_session.py` | visitor token and check-in behaviour |
| `tango_chat_recorder.py` | separate chat capture helper, not part of the plugin |
| `docs/superpowers/` | earlier design and plan for the recording/hardening round |

`profile.html` in the fixtures is a **stream** page; the name predates
profile tracking. The profile pages are `profile_page.html` (alias) and
`profile_page_account.html` (account id). Both mirror the live three-block
JSON-LD (Organization, WebSite, ProfilePage) but are scrubbed
reconstructions, not literal captures.

## 7. Commands

Run the suite with the interpreter that ships with yt-dlp, not the system
Python, or `yt_dlp` will not import:

```sh
cd ~/Projects/tango-yt-dlp-plugin
PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python \
    -m unittest discover -s tests
```

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
timeout -s INT 30 yt-dlp -f 1 --newline 'https://www.tango.me/@<alias>'
ffprobe -v error -show_entries format=duration:stream=codec_name,width,height \
    -of default=noprint_wrappers=1 ./*.mp4
```

A healthy run is 720x1280 (or similar), H.264 + AAC, and no `skipping`
lines in the log.

Debug switches that matter: `-v` for the extractor's own debug lines,
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
`@alias`, `%40alias` and account-id forms; account-id URL returns 5832
followers; offline profile raises the wait hint; 23–38 s recordings with 0
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

1. **Followers-only / private streams are unproven.**
   `_watch` maps 401/403 to a message naming `--cookies-from-browser chrome`,
   but no `isPrivateStream: true` entry has been observed live, so the path
   has never run against the real site. Next: find such a broadcast, note
   whether `watch` answers 401, 403 or a `FAILED_*` code, and add a fixture
   for whichever shape appears.

2. **A stream ending mid-recording was not re-observed on the windowed build.**
   The relay-side behaviour is unit-tested (`test_a_served_media_playlist_
   that_404s_ends_cleanly`, `test_ends_the_recording_when_the_token_stays_
   gone`), but the live case was last seen before the playlist window landed.
   Next: start a recording on a short broadcast, let it end, confirm the
   file finalises with the "stream has ended" warning and no error.

3. **Fixtures are scrubbed reconstructions.** They follow the live shapes but
   were not captured byte-for-byte. Next: capture
   `https://www.tango.me/<alias>` and `https://www.tango.me/<account-id>`,
   scrub ids/names/counters per `tests/fixtures/README.md`, replace the two
   profile page fixtures. Keep the three-block JSON-LD layout.

4. **Case sensitivity of aliases is unknown.** `TEST` and `test` are treated
   as different identifiers. Next: resolve a known alias with different
   casing; if the site folds case, normalise once in `_resolve_stream_id`
   and add a test. `[needs live check]`

5. **`no_proxy` is process-wide.** Fine for the CLI, wrong for embedded use.
   If the plugin is ever imported into a long-lived process, save the old
   value in `_exempt_loopback_from_proxy` and restore it in
   `TangoHlsRelay.close`.

6. **No CI yet.** Tests run by hand. The repo is public; a two-line workflow
   (install yt-dlp, run `unittest discover`) would make the suite a gate
   instead of a habit.

7. **yt-dlp issue #11433 is still open upstream.** Whether to post there is
   an operator decision, not a code change. Accepting a raw
   `master.m3u8?token=` URL stays out of scope: the token needs a stream id
   to refresh.

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
