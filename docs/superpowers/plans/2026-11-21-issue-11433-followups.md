# Follow-ups from yt-dlp issue #11433

Date: 2026-11-21
Branch: `fix/noscript-page-and-issue-notes`
Base: `main` at `c4b82ed`

## Why

Issue #11433 is still open after two years. Reading every comment plus live
probes (today) turns up one real defect in this plugin and several facts worth
recording, because people keep re-deriving them.

### Verified live today (2026-11-21)

| Probe | Result |
| --- | --- |
| `watch` for a live stream | returns the same 22-char id, `status: LIVING`, and `hlsStreamInfo.masterUrl` at `https://cinema-in41.tango.me/v2/<32 hex>/master.m3u8` |
| master URL, cookies only | `200` |
| master URL, `?token=<token>&username=<user>` and no cookies | `401` |
| master URL, nothing | `401` |
| `watch` with id `noscript.html` | HTTP 400, body `Unexpected server error: Illegal base64 character 2e` |
| `token-data` response headers | sets `tt`, `ttu`, `tte` with `Max-Age=10`, plus `datadome` |
| `https://tango.me/stream/noscript.html` today | 200, no server redirect (the 2024 redirect is gone) |
| plugin on `/stream/noscript.html`, `/live/noscript.html`, `/noscript.html` | `No tango.me profile found for "noscript.html"` (misleading) |
| plugin on the issue's dead stream id | `The stream has ended` (good) |
| plugin on the issue's alias (`<alias>`) | `No tango.me profile found for "<alias>"` (correct) |

## Task 1 — name the no-script page instead of resolving it

`_VALID_URL` matches `/stream/noscript.html` as `kind=stream`, `id=noscript.html`.
The id is not 22 characters, so `_resolve_stream_id` falls through to the alias
path and reports a missing *profile*, which is wrong and unhelpful.

**Change** in `yt_dlp_plugins/extractor/tango.py`:

- Add a module-level `_NON_CONTENT_ROUTES = frozenset({'noscript.html'})` next to
  `_WATCH_ERRORS`.
- In `TangoIE._real_extract`, before `_extract_stream`, compare the matched id
  case-insensitively against that set and raise an `ExtractorError(...,
  expected=True)` whose message says: tango served its no-JavaScript page, which
  carries no stream id; pass the original `https://www.tango.me/stream/<id>` or a
  profile URL.

No network call may happen before the raise.

**Acceptance**

- `/stream/noscript.html`, `/live/noscript.html` and `/noscript.html` all raise
  the expected error and make zero requests (`ydl.fake.requests == []`).
- The message contains `noscript.html` and the `https://www.tango.me/stream/`
  form.
- `ctx.exception.expected is True`.

## Task 2 — document what the issue establishes

`docs/kb/tango-me.md`: one new section, placed after *Live verification*, titled
`## yt-dlp issue #11433 (reviewed 2026-11-21)`, covering:

1. The reported symptom is generic extraction: tango redirected a client without
   JavaScript to `/stream/noscript.html`, yt-dlp followed it and reported
   `Unsupported URL`. The API path never reads that page, so it is immune; the
   route still exists and `watch` cannot parse it, so it now has its own error.
2. The CDN path hash is not the stream id (the issue's "this ID seems differ").
   The extractor reads `hlsStreamInfo.masterUrl`; building the path from the
   stream id would 404.
3. `?token=` alone is not authentication: 401 without the cookie triplet, 200
   with it. That explains why the pasted `master.m3u8` link dies ("stops after
   2-3 minutes") and why this plugin sends `tt`/`ttu`/`tte`.
4. `token-data` sets the triplet itself with `Max-Age=10`, matching the in-page
   ten-second poll; it also sets `datadome`, which this plugin drops.
5. `watch` answers protobuf unless `Accept: application/json` is sent.

`README.md`: a short `### Coming from yt-dlp issue #11433?` subsection under
*Use*, linking the issue, naming the `noscript.html` symptom and the
`master.m3u8?token=` workaround that expires, and saying what this plugin does
instead.

`docs/handoff-2026-11-21-tango-plugin.md`: record the review and the new error in
the state and remaining-work sections.

Style: plain declarative prose, no em dashes, no marketing. Facts and commands
exactly as measured.

## Task 3 — proof

```sh
cd ~/Projects/tango-yt-dlp-plugin
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
    /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
yt-dlp --simulate --no-warnings 'https://www.tango.me/stream/noscript.html'
```

The suite must be green (59 tests plus the new one) and the live command must
print the new message. No other behaviour may change; `git diff` must touch only
the four documents and `tango.py` plus the new test.

## Out of scope (recorded, not built)

- Accepting a raw `master.m3u8?token=` URL: decided and built later, see the
  follow-up branch `fix/cdn-url-and-account-session`. The token endpoint is
  not stream-scoped, so a copied playlist URL needs no stream id.
- Posting to the upstream issue: an operator decision, not a code change.
- Proving the followers-only path: still needs a private stream and a following
  account, already listed as open.
