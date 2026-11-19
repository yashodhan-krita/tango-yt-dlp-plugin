# Tango.me Extractor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_extract_stream` run and fail predictably without the network, then fix the retry, ending, logging, proxy and metadata defects the offline harness exposes.

**Architecture:** A fake yt-dlp transport handler serves captured fixtures, so the whole extraction path — resolve, check-in, watch, token, relay, manifest — runs in tests. Production fixes then land against that harness: retries via yt-dlp's own `RetryManager`, the relay on yt-dlp's HTTP stack, a clean end on CDN 404/410, loopback proxy exemption, quiet refresh, and a `timestamp`.

**Tech Stack:** Python 3.9+, yt-dlp plugin API (`yt_dlp.networking`, `InfoExtractor`, `RetryManager`), `unittest`, loopback `http.server` relay, ffmpeg downstream.

**Spec:** `docs/superpowers/specs/2026-11-20-tango-hardening-design.md`

## Global Constraints

- The CDN token lives 10 s; `TangoTokenRefresher.MARGIN` stays 2.0 s.
- Only playlists need auth; media segments are public and stay on the CDN.
- `watch` must precede every playlist fetch.
- No new runtime dependencies; the plugin keeps working on a bare yt-dlp install.
- The offline suite must pass with no network, no Chrome profile and no live stream, using:
  `PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests`
- Fixtures carry no tokens, account ids or personal data.
- Do not change `_rewrite_playlist`'s proven rewrite rules, the late-refresh policy, or the cached-playlist replay window.

## Review Focus

- A stream that ends while ffmpeg is between playlist reloads must finalise the file, not 502. Test lives in Task 5.
- A proxy configured only through `HTTP_PROXY` in the environment must not swallow the loopback relay. Test lives in Task 8.
- A profile page that 404s during a live stream must still produce a recording, titled from the stream id. Test lives in Task 6.
- An alias with no account must say so once, not after a retry storm. Tests live in Tasks 2 and 3.
- A 5xx from `watch` must be retried, then reported as the site's fault rather than a bad URL. Test lives in Task 2.

---

### Task 1: Fixture set and fake transport

**Files:**
- Create: `tests/fixtures/check_in.json`, `alias_convert.txt`, `stream_id.txt`, `watch.json`, `watch_wrong_id.json`, `watch_terminated.json`, `watch_bad_request.json`, `profile.html`, `feed.json`, `token.json`, `master.m3u8`, `hd.m3u8`, `ld.m3u8`, `README.md`
- Create: `tests/fake_net.py`
- Create: `tests/test_extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `TangoIE._ensure_session(video_id, force=False)` and `TangoBase._api_json(...)` as they exist today.
- Produces, in `tests/fake_net.py`:
  - `STREAM_ID = 'AAAABBBBCCCCDDDDEEEEFF'`, `ALIAS = 'tango_tester'`, `CDN = 'cinema-in41.tango.me'`
  - `fixture(name: str) -> bytes`, `json_fixture(name: str) -> bytes`
  - `@dataclass(frozen=True) class Reply: body: bytes; status: int = 200; headers: tuple[tuple[str, str], ...] = ()`
  - `fixture_reply(name: str, *, status: int = 200, headers=()) -> Reply`
  - `base_routes() -> dict[tuple[str, str, str], Reply | list[Reply]]`
  - `make_downloader(routes, params: dict | None = None) -> YoutubeDL`; the returned object has `.fake`, the `FakeRH` instance, whose `.requests` is a list of `yt_dlp.networking.Request`
  - `requests_to(ydl, path_fragment: str) -> list[Request]`
  - `class FakeRH(RequestHandler)` with `RH_KEY = 'Fake'`, `_SUPPORTED_URL_SCHEMES = ('http', 'https')`; import `UrllibRH` from `yt_dlp.networking._urllib` for the loopback fallback

Fixture values, used by every later task:

- `STREAM_ID` as above; account id `'GGGGHHHHIIIIJJJJKKKKLL'`; CDN hashes `'1' * 32` (master), `'2' * 32` (`hd`), `'3' * 32` (`ld`).
- `watch.json` mirrors `/tmp/watch.bin` with those ids, `"status": "LIVING"`, `"duration": "4885285"`, `"streamKind": "PUBLIC"`, `details.anchor.encryptedAccountId` = the account id, `viewerCount` 7, `uniqueViewerCount` 121, `tags: ["dating"]`, `settings` all false except `videoEnabled`/`audioEnabled`, and `viewInfo.hlsStreamInfo` with `masterUrl`/`hdUrl`/`ldUrl` on `CDN` and the three hashes.
- `watch_wrong_id.json`, `watch_terminated.json`, `watch_bad_request.json`: `{"code": "<CODE>", "details": {}}` with `FAILED_WRONG_STREAM_ID`, `FAILED_TERMINATED`, `FAILED_BAD_REQUEST`.
- `alias_convert.txt`: `GGGGHHHHIIIIJJJJKKKKLL` (no trailing newline). `stream_id.txt`: `AAAABBBBCCCCDDDDEEEEFF`.
- `token.json`: `{"token": "test-token", "username": "testuser", "expireAt": 4102444800}`.
- `check_in.json`: `{}`.
- `profile.html`: `<html><head><meta property="og:title" content="Tango Tester (@tango_tester)"><meta property="og:image" content="https://cget.tango.me/test.jpg"></head><body><script type="application/ld+json">{"mainEntity": {"name": "Tango Tester", "identifier": "tango_tester"}}</script></body></html>`
- `feed.json`: `{"catalogItems": [{"encryptedStreamId": STREAM_ID, "broadcaster": {"name": "Tango Tester"}}]}`.
- `master.m3u8`, `hd.m3u8`, `ld.m3u8`: copy the shapes from `PLAN.md` — two `#EXT-X-STREAM-INF` lines in the master pointing at `hd`/`ld` paths on `CDN`, and one-second `#EXTINF` media playlists pointing at `/v2/sh/...` segments.

- [ ] **Step 1: Capture and scrub the fixtures**

Write each file under `tests/fixtures/`, using the live API through a check-in cookie jar (`curl -c jar.txt -b jar.txt`). Replace ids, names, descriptions, thumbnails and hashes with the values above. Write `tests/fixtures/README.md` with one line per file: the exact method, URL, and request body that produced it, plus a note that `watch_terminated.json` and `watch_bad_request.json` are the observed `{"code": ...}` shapes from `PLAN.md` rather than fresh captures.

- [ ] **Step 2: Write the failing tests**

In `tests/test_extract.py`:

```python
def test_check_in_stores_the_visitor_token(self):
    ydl = make_downloader(base_routes())
    TangoIE(ydl)._ensure_session(STREAM_ID)
    self.assertIn('Tango-VT', {c.name for c in ydl.cookiejar})

def test_an_unrouted_request_is_loud(self):
    ydl = make_downloader({})
    with self.assertRaises(RequestError) as ctx:
        TangoIE(ydl)._api_json('/nope', STREAM_ID, note=False)
    self.assertIn('unrouted', str(ctx.exception))
```

- [ ] **Step 3: Run them to verify they fail**

Run: `PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest tests.test_extract -v`
Expected: `ModuleNotFoundError: No module named 'tests.fake_net'`.

- [ ] **Step 4: Implement `tests/fake_net.py`**

`FakeRH.validate(request)` calls `super().validate(request)` and then raises `UnsupportedRequest('not a tango host')` unless `urlparse(request.url).hostname` ends with `'tango.me'`, so the director falls through to `UrllibRH` for the loopback relay.

`FakeRH._send(request)`:
1. reads a `Cookie` header from `self._get_cookiejar(request).get_cookie_header(request.url)` and sets it on `request.headers` when the request has none;
2. looks up `(request.method, hostname, urlparse(request.url).path)` in `self.routes`, popping from a list when the value is a list and repeating the last `Reply` after that;
3. raises `RequestError(f'unrouted request: {method} {url}')` when nothing matches;
4. builds `email.message.Message` from `reply.headers` (duplicates preserved) and `Response(io.BytesIO(reply.body), request.url, headers, reply.status)`;
5. stores cookies with `self._get_cookiejar(request).extract_cookies(types.SimpleNamespace(info=lambda: response.headers), urllib.request.Request(request.url))`;
6. raises `HTTPError(response)` when `reply.status >= 400`.

`make_downloader(routes, params=None)` creates `YoutubeDL({'quiet': True, 'skip_download': True, **(params or {})})`, replaces `ydl._request_director` with `ydl.build_request_director([FakeRH, UrllibRH])`, assigns `routes` to the `FakeRH` instance, and sets `ydl.fake` to that instance.

`base_routes()` returns the route table described in the fixture values, keyed by `(method, host, path)`; `check_in` carries `Set-Cookie` for `Tango-VT=visitor-token; Path=/; Domain=www.tango.me` and `Tango-SD=visitor-session; Path=/; Domain=www.tango.me`, and every API route carries `Content-Type: application/json`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest tests.test_extract -v`
Expected: 2 tests pass.

- [ ] **Step 6: Run the whole suite and commit**

Run: `PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests`
Expected: 16 tests pass.

```bash
git add tests/fixtures tests/fake_net.py tests/test_extract.py
git commit -m "test: drive the extractor from captured tango fixtures"
```

---

### Task 2: Retries on control calls

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`TangoBase._api_json`, `_resolve_stream_id`, `_watch`, `TangoFeedIE._real_extract`)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `make_downloader`, `base_routes`, `Reply`, `fixture_reply`, `STREAM_ID` from Task 1.
- Produces: `TangoBase._retry(func, *, fatal=True) -> object | None`; `TangoBase._api_json(..., expected_status=None)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_transient_5xx_on_watch_is_retried(self):
    routes = base_routes()
    watch = ('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')
    routes[watch] = [Reply(b'busy', status=503), fixture_reply('watch.json')]
    ydl = make_downloader(routes)
    data = TangoIE(ydl)._watch(STREAM_ID)
    self.assertEqual(data['code'], 'OK')
    self.assertEqual(len(requests_to(ydl, '/watch')), 2)

def test_a_persistent_5xx_blames_the_site(self):
    routes = base_routes()
    routes[('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = Reply(b'busy', status=503)
    with self.assertRaises(ExtractorError) as ctx:
        TangoIE(make_downloader(routes))._watch(STREAM_ID)
    self.assertFalse(ctx.exception.expected)

def test_an_empty_alias_body_is_not_retried(self):
    routes = base_routes()
    routes[('GET', 'www.tango.me', '/api/visitors/profilealias/api/alias/convert')] = Reply(b'')
    ydl = make_downloader(routes)
    with self.assertRaises(ExtractorError) as ctx:
        TangoIE(ydl)._resolve_stream_id('nobody')
    self.assertIn('No tango.me profile found', str(ctx.exception))
    self.assertEqual(len(requests_to(ydl, '/alias/convert')), 1)
```

- [ ] **Step 2: Run to verify they fail**

Run: `... -m unittest tests.test_extract -v`
Expected: the 5xx tests fail with an error instead of a second attempt; the alias test shows more than one request.

- [ ] **Step 3: Implement**

Add to `TangoBase`:

```python
def _retry(self, func, *, fatal=True):
    for retry in self.RetryManager(fatal=fatal):
        try:
            return func()
        except ExtractorError as err:
            retry.error = err
    return None
```

give `_api_json` an `expected_status=None` parameter passed to `_download_json`, and wrap the four control calls with `fatal=True`:

- alias conversion and the live-stream lookup in `_resolve_stream_id`, both with `expected_status=404`, so a missing profile or an offline broadcaster stays a semantic answer (`ExtractorError('No tango.me profile found …')` / `UserNotLive`) and never a retry;
- the `watch` request in `_watch`;
- the catalog request in `TangoFeedIE._real_extract`.

Leave `_profile_metadata` exactly as it is (`fatal=False`).

- [ ] **Step 4: Run to verify they pass**

Run: `... -m unittest tests.test_extract -v`
Expected: all tests in the module pass.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_extract.py
git commit -m "fix: retry tango control calls before giving up"
```

---

### Task 3: Errors that name the cause

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`_watch`, `_resolve_stream_id`)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `_watch(stream_id) -> dict` from Task 2.
- Produces: nothing new; `_watch` still returns the stream document or raises `ExtractorError`/`UserNotLive`.

- [ ] **Step 1: Write the failing tests**

```python
WATCH_ERRORS = {
    'watch_wrong_id.json': 'No tango.me stream has that id',
    'watch_terminated.json': 'The stream has ended',
    'watch_bad_request.json': 'audio rooms are the usual cause',
}

def test_each_watch_code_has_its_own_message(self):
    for fixture_name, message in WATCH_ERRORS.items():
        routes = base_routes()
        routes[('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = fixture_reply(fixture_name)
        with self.subTest(fixture_name), self.assertRaises(ExtractorError) as ctx:
            TangoIE(make_downloader(routes))._watch(STREAM_ID)
        self.assertIn(message, str(ctx.exception))
        self.assertTrue(ctx.exception.expected)

def test_a_401_points_at_the_logged_in_retry(self):
    routes = base_routes()
    routes[('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = Reply(b'', status=401)
    with self.assertRaises(ExtractorError) as ctx:
        TangoIE(make_downloader(routes))._watch(STREAM_ID)
    self.assertIn('--cookies-from-browser chrome', str(ctx.exception))
    self.assertTrue(ctx.exception.expected)
```

- [ ] **Step 2: Run to verify they fail**

Expected: the four subtests report the current generic `Stream unavailable (...)` message; the 401 test reports the raw HTTP error.

- [ ] **Step 3: Implement**

In `_watch`, map the code before the generic check:

```python
_WATCH_ERRORS = {
    'FAILED_WRONG_STREAM_ID': 'No tango.me stream has that id',
    'FAILED_TERMINATED': 'The stream has ended',
    'FAILED_BAD_REQUEST': 'tango.me refused the stream lookup; audio rooms are the usual cause',
}
```

Raise `ExtractorError(message, expected=True)` for a mapped code, `ExtractorError(f'tango.me returned {code or "no data"}', expected=True)` otherwise. Catch the `ExtractorError` coming out of the retried request and, when `getattr(err.cause, 'status', None) in (401, 403)`, re-raise `ExtractorError('tango.me refused the stream lookup. For a followers-only or private stream, retry with --cookies-from-browser chrome', expected=True, cause=err)`. In `_resolve_stream_id`, keep "No tango.me profile found for \"<alias>\"" for an empty alias conversion and `UserNotLive(video_id=alias_or_id)` for an empty stream id.

- [ ] **Step 4: Run to verify they pass**

Expected: both tests pass; `test_each_watch_code_has_its_own_message` covers three cases.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_extract.py
git commit -m "fix: name the tango lookup failure instead of reporting a code"
```

---

### Task 4: Relay on yt-dlp's HTTP stack

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`TangoHlsRelay.fetch`)
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `TangoHlsRelay(refresher, master_url)`; `refresher.ie._downloader.urlopen(request)`.
- Produces: `fetch(path) -> (int, str, bytes)` unchanged. The upstream request is a `yt_dlp.networking.common.Request` with `extensions={'timeout': 30}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_relay.py`:

```python
class RecordingDownloader:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
    def urlopen(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return playlist_response(outcome)

def relay_with(outcomes):
    refresher = FakeRefresher()
    refresher.ie = types.SimpleNamespace(
        _downloader=RecordingDownloader(outcomes), report_warning=mock.Mock())
    return TangoHlsRelay(refresher, 'https://cinema-in41.tango.me/v2/x/master.m3u8'), refresher

def test_the_upstream_request_carries_a_timeout_and_the_token(self):
    relay, _ = relay_with([MASTER])
    relay.fetch(relay.master_path)
    request = relay.refresher.ie._downloader.requests[0]
    self.assertEqual(request.extensions['timeout'], 30)
    self.assertEqual(request.headers['Cookie'], 'tt=t; ttu=u; tte=1')
    self.assertIn('https://www.tango.me/', request.headers['Referer'])

def test_a_transport_error_forces_a_refresh(self):
    relay, refresher = relay_with([TransportError('reset'), MASTER])
    status, _, _ = relay.fetch(relay.master_path)
    self.assertEqual(status, 200)
    self.assertTrue(refresher.forced)
```

`playlist_response(body)` returns `Response(io.BytesIO(body), 'https://cdn/x', Message() | {'Content-Type': ...}, 200)`; `FakeRefresher` gains a `forced` flag set by `ensure(force=True)`. Rewrite the four `ThrottlingFallbackTest` cases to the same seam: replace `mock.patch('urllib.request.urlopen', ...)` with `relay_with([...])`, and replace the `urllib.error.HTTPError` in `_throttled()` with a yt-dlp `HTTPError` whose response carries status 403. The assertions do not change.

- [ ] **Step 2: Run to verify they fail**

Run: `... -m unittest tests.test_relay -v`
Expected: `RecordingDownloader` is never called; the request assertions fail on `urllib.request.Request`.

- [ ] **Step 3: Implement**

Replace the `urllib.request.Request`/`urlopen` pair with `yt_dlp.networking.common.Request` and `self.refresher.ie._downloader.urlopen`, keeping the same headers (`Cookie`, `Origin`, `Referer`, `Accept`), the same two-attempt loop with `refresher.ensure(force=True)`, and the same catch list widened to `yt_dlp.networking.exceptions.HTTPError` and `TransportError`. Read `response.read()` and `response.status`; keep `response.headers.get('Content-Type')` as the default `'text/plain'`.

- [ ] **Step 4: Run to verify they pass, then the whole suite**

Expected: relay tests pass, 23 tests overall.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_relay.py
git commit -m "fix: fetch relay playlists through yt-dlp's HTTP stack"
```

---

### Task 5: End the recording when the CDN ends the stream

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`TangoHlsRelay.fetch`, `_rewrite_playlist`, `_end_playlist` area)
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `fetch` from Task 4; `_end_playlist(body) -> bytes`.
- Produces: `TangoHlsRelay._served: set[str]` of paths the relay served or emitted; `_end_playlist` unchanged; a module constant `_ENDED_MEDIA_PLAYLIST = b'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:1\n#EXT-X-ENDLIST\n'`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_served_media_playlist_that_404s_ends_cleanly(self):
    relay, refresher = relay_with([LIVE_MASTER, MEDIA, http_404()])
    relay.fetch(relay.master_path)     # emits MEDIA_PATH
    relay.fetch(MEDIA_PATH)            # serves and caches it
    status, _, body = relay.fetch(MEDIA_PATH)
    self.assertEqual(status, 200)
    self.assertTrue(body.endswith(b'#EXT-X-ENDLIST\n'))
    refresher.ie.report_warning.assert_called_once()

def test_a_path_the_relay_never_served_still_raises(self):
    relay, _ = relay_with([http_404()])
    with self.assertRaises(HTTPError):
        relay.fetch('/v2/unknown/x.m3u8')

def test_the_ended_playlist_is_reused(self):
    relay, refresher = relay_with([LIVE_MASTER, MEDIA, http_404()])
    relay.fetch(relay.master_path)
    relay.fetch(MEDIA_PATH)
    relay.fetch(MEDIA_PATH)
    relay.fetch(MEDIA_PATH)
    self.assertEqual(len(refresher.ie._downloader.requests), 3)
```

`MEDIA_PATH = '/v2/hd/live.m3u8?m=x'`. Add a `LIVE_MASTER` constant whose single variant is `MEDIA_PATH`, and leave the existing `MASTER` untouched so the rewrite tests keep their exact expected URIs. `MEDIA` must point at a segment on the same host. `http_404()` returns `HTTPError(response_with_status(404))`.

- [ ] **Step 2: Run to verify they fail**

Expected: each 404 raises out of `fetch`; the persisted-end assertions fail on the extra request count.

- [ ] **Step 3: Implement**

Record served paths: after a successful fetch add the requested path; in `_rewrite_playlist`, add every URI it emits. Check status 404 or 410 before deciding to retry, so an ended stream does not burn a token refresh. When `path in self._served`:

- take the cached body for `path` if present, otherwise `_ENDED_MEDIA_PLAYLIST`;
- pass it through `_end_playlist`, store it in `self._cache[path]`, warn once
  (`'The stream has ended (the CDN answered 404); finishing the recording'`) and return `200, 'application/vnd.apple.mpegurl', body`.

Otherwise keep the current two-attempt loop, replay and `raise last_error`. `_end_playlist` stays a no-op for master playlists and for bodies already ending.

- [ ] **Step 4: Run to verify they pass, then the whole suite**

Expected: relay tests pass, 26 tests overall.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_relay.py
git commit -m "fix: finish the recording when the CDN reports the stream gone"
```

---

### Task 6: Offline end-to-end extraction

**Files:**
- Modify: `tests/test_extract.py` (tests only; no production change expected)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `make_downloader(routes)`, `base_routes()`, `fixture_reply`, `Reply`, `STREAM_ID`, `ALIAS`, `CDN`; `TangoIE(ydl)._real_extract(url)`, `TangoFeedIE(ydl)._real_extract(url)`.
- Produces: nothing production-facing. These tests are the regression net for the whole path.

- [ ] **Step 1: Write the tests**

```python
def test_a_stream_extracts_from_the_fixtures(self):
    info = TangoIE(make_downloader(base_routes()))._real_extract(
        f'https://www.tango.me/stream/{STREAM_ID}')
    self.assertEqual(info['id'], STREAM_ID)
    self.assertIs(info['is_live'], True)
    self.assertEqual(info['uploader'], 'Tango Tester')
    self.assertEqual(info['uploader_id'], ALIAS)
    self.assertEqual(info['channel_url'], f'https://www.tango.me/{ALIAS}')
    self.assertTrue(info['formats'])
    self.assertEqual(urlparse(info['formats'][0]['url']).hostname, '127.0.0.1')
    self.assertEqual(urlparse(info['formats'][0]['manifest_url']).hostname, '127.0.0.1')

def test_alias_and_at_alias_reach_the_same_stream(self):
    for entry in (ALIAS, f'@{ALIAS}'):
        info = TangoIE(make_downloader(base_routes()))._real_extract(
            f'https://www.tango.me/{entry}')
        self.assertEqual(info['id'], STREAM_ID)

def test_the_account_session_survives_the_check_in(self):
    ydl = make_downloader(base_routes())
    ydl.cookiejar.set_cookie(account_cookie('Tango-ST'))
    TangoIE(ydl)._real_extract(f'https://www.tango.me/stream/{STREAM_ID}')
    names = {(c.domain, c.name) for c in ydl.cookiejar}
    self.assertIn(('.www.tango.me', 'Tango-ST'), names)
    self.assertIn(('www.tango.me', 'Tango-VT'), names)

def test_an_existing_visitor_token_skips_the_check_in(self):
    ydl = make_downloader(base_routes())
    ydl.cookiejar.set_cookie(visitor_cookie(time.time() + 3600))
    TangoIE(ydl)._real_extract(f'https://www.tango.me/stream/{STREAM_ID}')
    self.assertEqual(len(requests_to(ydl, 'check-in')), 0)

def test_a_missing_profile_page_does_not_stop_the_recording(self):
    routes = base_routes()
    routes[('GET', 'www.tango.me', f'/stream/{STREAM_ID}')] = Reply(b'', status=404)
    info = TangoIE(make_downloader(routes))._real_extract(
        f'https://www.tango.me/stream/{STREAM_ID}')
    self.assertEqual(info['title'], f'{STREAM_ID} - Tango live')

def test_the_feed_lists_the_captured_stream(self):
    feed = TangoFeedIE(make_downloader(base_routes()))._real_extract(
        'https://www.tango.me/live')
    self.assertEqual([entry['id'] for entry in feed['entries']], [STREAM_ID])
```

`account_cookie(name)` builds a `http.cookiejar.Cookie` on `.www.tango.me`; `visitor_cookie(exp)` builds a `Tango-VT` JWT on `www.tango.me` (reuse the `_jwt` helper shape from `tests/test_session.py`).

- [ ] **Step 2: Run to see which fail**

Run: `... -m unittest tests.test_extract -v`
Expected: failures expose any remaining network dependency in the path. Fix only the harness (`fake_net.py`, routes, fixture scrubbing) in this task; if a failure is a production defect, fold its fix into Task 4 or 5 rather than editing the extractor here without a test-first step.

- [ ] **Step 3: Run to verify they pass**

Expected: all `test_extract` tests pass; the whole suite is 32 tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_extract.py tests/fake_net.py tests/fixtures
git commit -m "test: cover the whole tango extraction path offline"
```

---

### Task 7: A timestamp for live recordings

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`_extract_stream`)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `info` from `_extract_stream(url, video_id)`.
- Produces: `info['timestamp']: int` (Unix seconds) for a live stream; absent for the finished-recording fallback.

- [ ] **Step 1: Write the failing test**

```python
def test_a_live_stream_carries_its_start_time(self):
    info = TangoIE(make_downloader(base_routes()))._real_extract(
        f'https://www.tango.me/stream/{STREAM_ID}')
    self.assertAlmostEqual(info['timestamp'], int(time.time()) - 4885, delta=5)
```

- [ ] **Step 2: Run to verify it fails**

Expected: `KeyError: 'timestamp'`.

- [ ] **Step 3: Implement**

In `_extract_stream`, when `is_live`, read `stream.get('duration')` through `int_or_none` and set `'timestamp': int(time.time()) - duration_ms // 1000` on the info dict. No timestamp when the value is missing or when the stream is not live.

- [ ] **Step 4: Run to verify it passes**

Expected: the test passes.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_extract.py
git commit -m "feat: give live tango recordings a start timestamp"
```

---

### Task 8: Keep the loopback relay out of the proxy

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (new module function; called from `_extract_stream`)
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces: `_exempt_loopback_from_proxy(environ=None) -> None`, mutating `os.environ` or the given mapping by appending `127.0.0.1` and `localhost` to `no_proxy` and `NO_PROXY` when absent.

- [ ] **Step 1: Write the failing test**

```python
def test_loopback_is_exempt_from_the_proxy(self):
    with mock.patch.dict(os.environ, {'no_proxy': 'corp.example'}, clear=True):
        _exempt_loopback_from_proxy()
        self.assertEqual(os.environ['no_proxy'], 'corp.example,127.0.0.1,localhost')
        self.assertEqual(os.environ['NO_PROXY'], '127.0.0.1,localhost')
        _exempt_loopback_from_proxy()
        self.assertEqual(os.environ['no_proxy'], 'corp.example,127.0.0.1,localhost')
```

- [ ] **Step 2: Run to verify it fails**

Expected: `ImportError: cannot import name '_exempt_loopback_from_proxy'`.

- [ ] **Step 3: Implement**

Empty and absent values are skipped; entries are trimmed; duplicates are not added. Call it once in `_extract_stream` right after the relay is constructed.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Expected: 34 tests pass.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_extract.py
git commit -m "fix: exempt the loopback relay from HTTP proxies"
```

---

### Task 9: Quiet token refresh

**Files:**
- Modify: `yt_dlp_plugins/extractor/tango.py` (`TangoTokenRefresher.refresh`)
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `TangoTokenRefresher(ie, video_id)` with `ie._issue_token(video_id, note=...)` and `ie.write_debug(msg)`.
- Produces: `refresh() -> None`, calling `_issue_token` with `note=None`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_refresh_does_not_print_a_progress_line(self):
    ie = mock.Mock()
    ie._issue_token.return_value = {'token': 't', 'username': 'u', 'expireAt': 2 ** 31}
    TangoTokenRefresher(ie, 'v').refresh()
    self.assertIsNone(ie._issue_token.call_args.kwargs['note'])
    ie.write_debug.assert_called_once()
```

- [ ] **Step 2: Run to verify it fails**

Expected: `note` is `'Refreshing stream token'`.

- [ ] **Step 3: Implement**

Pass `note=None` and follow with `self.ie.write_debug(f'Stream token refreshed (expires at {int(self.expires)})')`. Keep the `_store` call unchanged. Add the `mock` and `TangoTokenRefresher` imports to `tests/test_session.py`.

- [ ] **Step 4: Run to verify it passes**

Expected: `tests.test_session` passes.

- [ ] **Step 5: Commit**

```bash
git add yt_dlp_plugins/extractor/tango.py tests/test_session.py
git commit -m "fix: stop printing a token refresh line every eight seconds"
```

---

### Task 10: Package install and docs

**Files:**
- Modify: `README.md`, `PLAN.md`, `pyproject.toml` (only if the install check fails)
- Test: manual, recorded in the commit message

**Interfaces:**
- Consumes: everything above.
- Produces: none.

- [ ] **Step 1: Build and install the wheel**

```bash
python3 -m venv /tmp/tango-build
/tmp/tango-build/bin/pip install --quiet build
/tmp/tango-build/bin/python -m build --wheel
python3 -m venv /tmp/tango-venv
/tmp/tango-venv/bin/pip install --quiet yt-dlp dist/*.whl
/tmp/tango-venv/bin/yt-dlp --list-extractors | grep -E '^tango'
```

Expected: both `tango` and `tango:live` are listed. If they are not, fix `[project.entry-points]` or the package discovery in `pyproject.toml` and repeat.

- [ ] **Step 2: Update the docs**

`README.md`: document `timestamp`/`upload_date`, the `--proxy` behaviour (loopback is exempted, upstream is proxied), and the ended-stream behaviour under Limits. `PLAN.md`: move the fixed open items (upstream 404, control-call retries, timestamp, refresh logging, proxy) out of "Open items" and into "Design decisions" or "Measurements", leaving followers-only, audio rooms and chat untouched.

- [ ] **Step 3: Run the full suite one last time**

Run: `PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add README.md PLAN.md pyproject.toml
git commit -m "docs: record the hardened behaviour and verify pip installs"
```

---

## Live verification (after Task 10)

Not part of the offline gate; run once when a live stream is available and paste the evidence into the session or an audit entry:

- `yt-dlp -f best --plugin-dirs /Users/kritananda/Projects <alias-url>` for ≥120 s, SIGINT, then `ffprobe` (H.264 + AAC, 720x1280).
- The same by `@alias` and by stream id, plus one `--cookies-from-browser chrome` run.
- `yt-dlp -F` shows a non-empty `upload_date`/`timestamp` for a live stream.
- A run with `--proxy` pointed at a real HTTP proxy starts and records.
- A stream that ends mid-recording finalises with ENDLIST; the file plays.
