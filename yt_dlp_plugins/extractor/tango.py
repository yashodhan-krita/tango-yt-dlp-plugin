"""yt-dlp extractor for tango.me live streams.

The public web player is a canvas player fed by HLS.  Everything needed to
record a stream is reachable anonymously through the site's own web API:

    check-in            -> visitor cookies (Tango-VT / Tango-SD / Tango-DeviceId)
    .../stream/{id}/watch -> registers this viewer for the stream and returns the
                              stream document (settings, status, all HLS URLs)
    token-data             -> short-lived CDN token ({token, username, expireAt})

The CDN (``cinema-*.tango.me``) protects *playlists* with a cookie triplet
``tt``/``ttu``/``tte`` that expires after ~10 seconds, while media *segments*
are public.  Left alone, a live recording therefore dies a few seconds in, so
the token is re-issued on demand for as long as downloading continues.

A logged-in browser session (``--cookies-from-browser chrome``) is honoured for
streams a visitor cannot see; the token refresh still runs on top of it.
"""

import base64
import http.server
import json
import re
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    UserNotLive,
    int_or_none,
    traverse_obj,
    url_or_none,
)

_API_BASE = 'https://www.tango.me/api'

_STREAM_ID_RE = r'[A-Za-z0-9_-]{22}'


class TangoTokenRefresher:
    """Keeps the CDN auth cookies fresh.

    The CDN accepts playlists only while the cookie triplet ``tt``/``ttu``/
    ``tte`` is unexpired, and denies it after roughly ten seconds.  The token
    is therefore re-issued on demand; :class:`TangoHlsRelay` asks for one
    every time ffmpeg reloads a playlist.

    The triplet is sent by the relay as a request header rather than stored in
    yt-dlp's cookie jar: ffmpeg has no jar, so the jar would only leak the
    cookies into unrelated requests.
    """

    # Refresh this many seconds before the token expires.  The token lives ten
    # seconds and the endpoint is rate limited, so refresh late but not last.
    MARGIN = 2.0

    def __init__(self, ie, video_id):
        self.ie = ie
        self.video_id = video_id
        self.expires = 0.0
        self.username = None
        self._token = None
        self._lock = threading.Lock()
        self._refreshing = False

    def _store(self, token, username, expires):
        self._token, self.username, self.expires = token, username, float(expires)

    def cookie_header(self):
        self.ensure()
        return f'tt={self._token}; ttu={self.username}; tte={int(self.expires)}'

    def refresh(self):
        data = self.ie._issue_token(self.video_id, note='Refreshing stream token')
        self._store(data['token'], data['username'], data['expireAt'])

    def ensure(self, force=False):
        with self._lock:
            if self._refreshing:
                return
            if not force and time.time() < self.expires - self.MARGIN:
                return
            self._refreshing = True
            try:
                self.refresh()
            except Exception as err:  # noqa: BLE001 - never break a request over this
                self.ie.report_warning(f'Could not refresh stream token: {err}')
            finally:
                self._refreshing = False


def _end_playlist(body):
    """Mark a cached media playlist as finished, so ffmpeg stops cleanly."""
    if b'#EXT-X-STREAM-INF' in body or b'#EXT-X-ENDLIST' in body:
        return body
    return body.rstrip(b'\n') + b'\n#EXT-X-ENDLIST\n'


class _TangoRelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def _respond(self, send_body):
        try:
            status, content_type, body = self.server.relay.fetch(self.path)
        except Exception as err:  # noqa: BLE001
            self.send_error(502, str(err))
            return
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        self._respond(send_body=True)

    def do_HEAD(self):
        self._respond(send_body=False)

    def log_message(self, *args):
        pass


class _TangoRelayServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TangoHlsRelay:
    """Local HLS relay in front of the tango CDN.

    yt-dlp always hands live HLS to ffmpeg, and ffmpeg cannot refresh
    cookies, so its playlist reloads would die with the token.  ffmpeg is
    pointed at this loopback server instead: the HLS manifest is fetched with
    a fresh token every time, and the (unauthenticated) media segments are
    rewritten to go straight to the CDN.
    """

    IDLE_TIMEOUT = 900

    # If the token endpoint throttles us, keep ffmpeg alive by replaying the
    # last playlist that did load.  Measured throttles lasted ~90 s, and the
    # segments in it are public, so replay costs duplicates rather than loss.
    STALE_PLAYLIST_MAX_AGE = 120

    def __init__(self, refresher, master_url):
        self.refresher = refresher
        parts = urllib.parse.urlsplit(master_url)
        self.upstream = f'{parts.scheme}://{parts.netloc}'
        self.master_path = parts.path + (f'?{parts.query}' if parts.query else '')
        self._last_request = time.time()
        self._cache = {}
        self._warned_stale = False
        self._server = _TangoRelayServer(('127.0.0.1', 0), _TangoRelayHandler)
        self._server.relay = self
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name='tango-hls-relay').start()
        threading.Thread(target=self._reap, daemon=True,
                         name='tango-hls-relay-reaper').start()

    def url(self):
        return f'http://127.0.0.1:{self.port}{self.master_path}'

    def close(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _reap(self):
        while True:
            time.sleep(60)
            if time.time() - self._last_request > self.IDLE_TIMEOUT:
                self.close()
                return

    def fetch(self, path):
        self._last_request = time.time()
        url = f'{self.upstream}{path}'
        last_error = None
        # Two tries: the token may have expired between playlist reloads.  The
        # token path already retries internally, so more here only adds load
        # to a throttled endpoint.
        for attempt in range(2):
            if attempt:
                self.refresher.ensure(force=True)
                time.sleep(1)
            request = urllib.request.Request(url, headers={
                'Cookie': self.refresher.cookie_header(),
                'Origin': 'https://www.tango.me',
                'Referer': 'https://www.tango.me/',
                'Accept': '*/*',
            })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    content_type = response.headers.get('Content-Type') or 'text/plain'
                break
            except urllib.error.HTTPError as err:
                last_error = err
                if err.code not in (401, 403, 500, 502, 503, 504):
                    raise
            except urllib.error.URLError as err:
                last_error = err
        else:
            cached = self._cache.get(path)
            if cached:
                age = time.time() - cached[2]
                if age < self.STALE_PLAYLIST_MAX_AGE:
                    if not self._warned_stale:
                        self.refresher.ie.report_warning(
                            'Stream token refresh is being throttled; replaying '
                            'the last playlist until it recovers')
                        self._warned_stale = True
                    return 200, cached[1], cached[0]
                # Too long without a token.  End the playlist so ffmpeg
                # finalises the file instead of failing a long recording.
                self.refresher.ie.report_warning(
                    'Could not refresh the stream token for over '
                    f'{self.STALE_PLAYLIST_MAX_AGE}s; ending the recording '
                    'cleanly')
                return 200, cached[1], _end_playlist(cached[0])
            raise last_error

        self._warned_stale = False

        if body.lstrip().startswith(b'#EXTM3U'):
            body = self._rewrite_playlist(body, url)
            content_type = 'application/vnd.apple.mpegurl'
        self._cache[path] = (body, content_type, time.time())
        return 200, content_type, body

    def _rewrite_playlist(self, body, upstream_url):
        """Point ffmpeg at the CDN for segments, at the relay for playlists.

        Media segments are public, so ffmpeg can fetch them directly (no
        Python in the video path).  Variant playlists still need a fresh
        token, so they stay behind the relay.
        """
        text = body.decode('utf-8', 'replace')
        if '#EXT-X-STREAM-INF' in text:
            def relay_relative(match):
                uri = urllib.parse.urljoin(upstream_url, match.group(1))
                parts = urllib.parse.urlsplit(uri)
                return parts.path + (f'?{parts.query}' if parts.query else '')

            return re.sub(r'^(?!#)(\S+)$', relay_relative, text,
                          flags=re.MULTILINE).encode()

        def absolute(match):
            prefix, uri, suffix = match.groups()
            return f'{prefix}{urllib.parse.urljoin(upstream_url, uri)}{suffix}'

        text = re.sub(r'(#EXT-X-(?:MAP|KEY):[^\n]*?URI=")([^"]+)(")',
                      absolute, text)
        lines = []
        for line in text.splitlines():
            if line and not line.startswith('#'):
                line = urllib.parse.urljoin(upstream_url, line)
            lines.append(line)
        return ('\n'.join(lines) + '\n').encode()


class TangoBase(InfoExtractor):
    _API_BASE = _API_BASE

    # ------------------------------------------------------------------ HTTP

    def _api_headers(self, **extra):
        headers = {
            'Accept': 'application/json',
            'Origin': 'https://www.tango.me',
            'Referer': 'https://www.tango.me/',
            'foreground-id': str(uuid.uuid4()),
            'interaction-id': str(uuid.uuid4()),
            'x-app-client-session-id': str(uuid.uuid4()),
        }
        headers.update(extra)
        return headers

    def _api_json(self, path, video_id, note, query=None, data=None,
                  headers=None, fatal=True):
        return self._download_json(
            f'{self._API_BASE}{path}', video_id, note=note,
            query=query, data=data, fatal=fatal,
            headers=self._api_headers(**(headers or {})),
        )

    # --------------------------------------------------------------- session

    # Cookies that only make sense to the JavaScript player.  ``datadome`` /
    # ``_dd_s`` are bound to the browser's TLS fingerprint; replaying them
    # from Python invites a bot challenge.
    _DROP_COOKIES = {'datadome', '_dd_s'}

    # Cookies that identify the anonymous visitor.  The token endpoint
    # throttles per identity, and a fresh identity recovers where a retry of
    # the throttled one keeps getting 403.
    _VISITOR_COOKIES = {'Tango-VT', 'Tango-SD', 'Tango-DI', 'Tango-DeviceId'}

    def _drop_cookies(self, names, keep_domain_prefix=None):
        jar = self._downloader.cookiejar
        for cookie in list(jar):
            if cookie.name not in names and cookie.name.lower() not in names:
                continue
            if keep_domain_prefix and cookie.domain.startswith(keep_domain_prefix):
                continue
            try:
                jar.clear(cookie.domain, cookie.path, cookie.name)
            except KeyError:
                pass

    def _drop_bot_cookies(self):
        self._drop_cookies(self._DROP_COOKIES)

    def _reset_visitor(self):
        """Forget the anonymous identity, keeping any account session.

        A logged-in browser stores its session descriptor on ``.www.tango.me``
        while the visitor one lands on ``www.tango.me``, so the dot-prefixed
        cookie is kept.
        """
        self._drop_cookies(self._VISITOR_COOKIES, keep_domain_prefix='.www.')

    def _has_visitor_token(self):
        """Whether the jar holds an unexpired visitor JWT (``Tango-VT``)."""
        for cookie in self._downloader.cookiejar:
            if cookie.name == 'Tango-VT' and cookie.value:
                try:
                    payload = cookie.value.split('.')[1]
                    payload += '=' * (-len(payload) % 4)
                    expires = json.loads(base64.urlsafe_b64decode(payload)).get('exp')
                except (IndexError, ValueError, json.JSONDecodeError):
                    return True
                return not expires or float(expires) > time.time()
        return False

    def _check_in(self, video_id, note='Registering visitor'):
        data = self._api_json(
            '/visitor-lobby/api/public/visitors/v1/check-in', video_id,
            note=note, query={'appId': 'tango'},
            data=b'{}', headers={'Content-Type': 'application/json'})
        if not isinstance(data, dict):
            raise ExtractorError('tango.me visitor registration failed')

    def _ensure_session(self, video_id, force=False):
        """Make sure the cookie jar holds a visitor identity.

        The token endpoint is a *visitor* endpoint: even a logged-in browser
        session has to check in before it can be issued a CDN token.  Account
        cookies are left untouched, so a private stream still sees them.
        """
        self._drop_bot_cookies()
        if not force and self._has_visitor_token():
            return
        if force:
            self._reset_visitor()
        self._check_in(video_id, note=('Re-registering visitor' if force
                                       else 'Registering visitor'))

    def _issue_token(self, video_id, note='Obtaining stream token'):
        """Fetch a CDN token, retrying with a fresh visitor identity.

        The endpoint answers 403 when it throttles an identity.  A new visitor
        check-in gets a token where retrying the throttled identity does not,
        so the second attempt resets it.  Errors are swallowed here and
        reported once by the caller, to keep the log readable.
        """
        last_error = None
        for attempt in range(3):
            try:
                data = self._api_json(
                    '/stream/token-data/v1/web/visitor', video_id,
                    note=note if not attempt else 'Retrying stream token')
            except ExtractorError as err:
                last_error = err
                data = None
            if isinstance(data, dict) and data.get('token'):
                return {
                    'token': data['token'],
                    'username': data.get('username') or '',
                    'expireAt': int(data['expireAt']),
                }
            if attempt == 0:
                try:
                    self._ensure_session(video_id, force=True)
                except ExtractorError as err:
                    last_error = err
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise last_error or ExtractorError('tango.me did not return a stream token')

    # -------------------------------------------------------------- resolving

    def _resolve_stream_id(self, alias_or_id):
        """Accept a 22-char stream id, a profile alias, or a shared ``@alias``."""
        alias_or_id = (alias_or_id or '').lstrip('@')
        if re.fullmatch(_STREAM_ID_RE, alias_or_id):
            return alias_or_id
        account_id = (self._download_webpage(
            f'{self._API_BASE}/visitors/profilealias/api/alias/convert',
            alias_or_id, note='Resolving profile alias',
            query={'alias': alias_or_id}, fatal=False,
            headers=self._api_headers()) or '').strip()
        if not account_id:
            raise ExtractorError(
                f'No tango.me profile found for "{alias_or_id}"', expected=True)
        stream_id = (self._download_webpage(
            f'{self._API_BASE}/showroom-streams/api/public/stream/v1/id',
            alias_or_id, note='Looking for a live stream',
            query={'broadcasterId': account_id}, fatal=False,
            headers=self._api_headers()) or '').strip()
        if not stream_id:
            raise UserNotLive(video_id=alias_or_id)
        return stream_id

    def _watch(self, stream_id):
        """Register as a viewer and return the stream document.

        This call is what authorises the CDN; without it the playlists answer
        401 even with a valid token.
        """
        data = self._api_json(
            f'/visitors/stream/client/v2/stream/{stream_id}/watch', stream_id,
            note='Joining stream', query={'requestId': ''},
            data=b'', headers={'Content-Type': 'application/json'}, fatal=False)
        if not isinstance(data, dict):
            raise ExtractorError('tango.me stream lookup failed', expected=True)
        if data.get('code') != 'OK' or not data.get('details', {}).get('stream'):
            raise UserNotLive(video_id=stream_id,
                              msg=f'Stream unavailable ({data.get("code") or "no data"})')
        return data

    def _profile_metadata(self, video_id):
        """Best-effort metadata from the server-rendered stream/profile page."""
        html = self._download_webpage(
            f'https://www.tango.me/stream/{video_id}', video_id,
            note='Downloading stream page', fatal=False) or ''
        title = self._og_search_property('title', html, default='') or ''
        name = alias = None
        for block in re.findall(
                r'<script[^>]+type="application/ld\+json"[^>]*>\s*(.*?)\s*</script>',
                html, re.DOTALL):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get('mainEntity'), dict):
                entity = data['mainEntity']
                name = entity.get('name') or name
                alias = entity.get('identifier') or alias
        if not alias:
            match = re.search(r'\(@([^)\s]+)\)', title)
            if match:
                alias = match.group(1)
        if not name:
            name = re.sub(r'\s*\(@[^)]*\).*$', '', title).strip() or None
        return {
            'name': name,
            'alias': alias,
            'description': self._og_search_property(
                'description', html, default=None),
            'thumbnail': url_or_none(self._og_search_property(
                'image', html, default=None)),
        }

    # --------------------------------------------------------------- formats

    def _hls_formats(self, playlist_url, video_id, live):
        formats, subtitles = self._extract_m3u8_formats_and_subtitles(
            playlist_url, video_id, ext='mp4', live=live,
            note='Downloading HLS manifest', fatal=False)
        for f in formats:
            f['http_headers'] = dict(f.get('http_headers') or {}, **{
                'Origin': 'https://www.tango.me',
                'Referer': 'https://www.tango.me/',
            })
        return formats, subtitles

    # ---------------------------------------------------------------- shared

    def _extract_stream(self, url, video_id):
        self._ensure_session(video_id)
        stream_id = self._resolve_stream_id(video_id)

        data = self._watch(stream_id)
        details = data.get('details') or {}
        stream = details.get('stream') or {}
        settings = details.get('settings') or {}
        view_info = details.get('viewInfo') or {}
        hls_info = view_info.get('hlsStreamInfo') or {}

        status = stream.get('status') or ''
        is_live = status == 'LIVING'
        if not is_live:
            complete = stream.get('completeListUrl')
            if not complete:
                raise UserNotLive(video_id=stream_id)
            self.report_warning(
                f'Stream is not live (status: {status or "unknown"}); '
                'falling back to the complete recording')

        profile = self._profile_metadata(stream_id)
        alias = profile.get('alias')
        name = profile.get('name') or alias or stream_id

        refresher = TangoTokenRefresher(self, stream_id)
        refresher.ensure(force=True)

        master_url = (
            traverse_obj(hls_info, 'masterUrl')
            or stream.get('masterListUrl')
            or stream.get('completeListUrl'))
        if not master_url:
            raise ExtractorError('tango.me returned no HLS URL', expected=True)

        sep = '&' if '?' in master_url else '?'
        master_url = f'{master_url}{sep}accountId=&tech=lhls&label=pwatcf'
        if refresher.username:
            master_url = f'{master_url}&username={refresher.username}'
        relay = TangoHlsRelay(refresher, master_url)
        formats, subtitles = self._hls_formats(
            relay.url(), stream_id, live=is_live)
        if not formats:
            kind = 'live stream' if is_live else 'recording'
            raise ExtractorError(
                f'The {kind} HLS manifest could not be read. '
                'For a followers-only or private stream retry with '
                '--cookies-from-browser chrome',
                expected=True)

        title = f'{name} (@{alias})' if alias and alias != name else name
        if is_live:
            title = f'{title} - Tango live'
        else:
            title = f'{title} - Tango recording'

        info = {
            'id': stream_id,
            'title': title,
            'formats': formats,
            'subtitles': subtitles,
            'is_live': is_live,
            'live_status': 'is_live' if is_live else 'was_live',
            'uploader': name,
            'uploader_id': alias,
            'channel': name,
            'channel_id': details.get('anchor', {}).get('encryptedAccountId'),
            'channel_url': f'https://www.tango.me/{alias}' if alias else None,
            'uploader_url': f'https://www.tango.me/{alias}' if alias else None,
            'webpage_url': url,
            'thumbnail': traverse_obj(stream, 'thumbnail') or profile.get('thumbnail'),
            'description': profile.get('description'),
            'concurrent_view_count': int_or_none(details.get('viewerCount')),
            'view_count': int_or_none(details.get('uniqueViewerCount')),
            'tags': traverse_obj(details, ('tags', ...)),
            'age_limit': 18,
        }
        if not settings.get('videoEnabled', True) or settings.get('audioRoom'):
            self.to_screen('This stream is audio-only')
        return info


class TangoFeedIE(TangoBase):
    """tango.me/live — the front-page feed of currently live streams."""

    IE_NAME = 'tango:live'
    _VALID_URL = r'https?://(?:www\.|m\.)?tango\.me/live/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://www.tango.me/live',
        'info_dict': {'id': 'live'},
        'playlist_mincount': 1,
        'params': {'extract_flat': True, 'skip_download': True},
    }]

    def _real_extract(self, url):
        self._ensure_session('live')
        data = self._api_json(
            '/showroom-streams/api/public/web/catalog/v1', 'live',
            note='Downloading live feed', query={'traceId': str(uuid.uuid4())},
            data=json.dumps({'size': 30, 'platform': 'web_desktop',
                             'hMarketingCampaign': False}).encode(),
            headers={'Content-Type': 'application/json'})
        items = traverse_obj(data, ('catalogItems', ...)) or []
        entries = []
        for item in items:
            stream_id = traverse_obj(item, 'encryptedStreamId')
            if not stream_id:
                continue
            broadcaster = traverse_obj(item, ('broadcaster', 'name')) or stream_id
            entries.append(self.url_result(
                f'https://www.tango.me/stream/{stream_id}', TangoIE,
                stream_id, f'{broadcaster} - Tango live'))
        if not entries:
            raise ExtractorError('No live streams on the tango.me feed', expected=True)
        return self.playlist_result(entries, 'live', 'Tango live feed')


class TangoIE(TangoBase):
    IE_NAME = 'tango'
    _VALID_URL = (
        r'https?://(?:www\.|m\.)?tango\.me/'
        r'(?:(?:stream|live|profile)/)?'
        r'(?!(?:live|stream|profile|search|explore|login|signup|gifts|store|settings|wallet)'
        r'/?(?:[?#]|$))'
        r'(?P<id>[^/?#&]+)')
    _TESTS = [{
        'url': 'https://www.tango.me/stream/jBLKPPFpEsXV2S2C7nC39A',
        'info_dict': {
            'id': 'jBLKPPFpEsXV2S2C7nC39A',
            'ext': 'mp4',
            'is_live': True,
        },
        'params': {'skip_download': True},
    }, {
        'url': 'https://www.tango.me/wisdomhood',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        return self._extract_stream(url, self._match_id(url))
