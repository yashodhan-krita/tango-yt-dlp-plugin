"""Offline fake transport for the tango.me extractor tests.

``FakeRH`` answers requests whose host ends in ``tango.me`` from a route table
of captured fixtures, and records every request it sees.  Anything else —
notably the loopback HLS relay — falls through to yt-dlp's ``UrllibRH``.
"""

from __future__ import annotations

import email.message
import io
import json
import types
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.networking._helper import wrap_request_errors
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.common import Features, Request, RequestHandler, Response
from yt_dlp.networking.exceptions import (
    HTTPError,
    RequestError,
    UnsupportedRequest,
)

STREAM_ID = 'AAAABBBBCCCCDDDDEEEEFF'
ALIAS = 'tango_tester'
CDN = 'cinema-in41.tango.me'

ACCOUNT_ID = 'GGGGHHHHIIIIJJJJKKKKLL'
_MASTER_HASH = '1' * 32
_HD_HASH = '2' * 32
_LD_HASH = '3' * 32
LIVE_HASH = '4' * 32

_FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def json_fixture(name: str) -> bytes:
    return json.dumps(json.loads(fixture(name))).encode()


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()


def fixture_reply(name: str, *, status: int = 200, headers=()) -> Reply:
    return Reply(fixture(name), status, tuple(headers))


CHECK_IN = ('POST', 'www.tango.me',
            '/api/visitor-lobby/api/public/visitors/v1/check-in')
TOKEN = ('GET', 'www.tango.me', '/api/stream/token-data/v1/web/visitor')
WATCH = ('POST', 'www.tango.me',
         f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')

_EPOCH = 'Expires=Thu, 01 Jan 1970 00:00:10 GMT'
RT_REFRESH_PATHS = ('/api/session-service/public/v2/session/web/refresh',
                    '/session-service/public/v2/session/web/refresh')


def check_in_reply(visitor_token: str = 'visitor-token',
                   body: bytes | None = None) -> Reply:
    return Reply(fixture('check_in.json') if body is None else body, 200, (
        # Host-only cookies, like the real check-in response: the visitor
        # identity lands on ``www.tango.me`` (no leading dot), separate from
        # the account session on ``.www.tango.me``.
        ('Set-Cookie', f'Tango-VT={visitor_token}; Path=/'),
        ('Set-Cookie', 'Tango-SD=visitor-session; Path=/'),
        # Measured live on 2026-11-21 with a logged-in Chrome profile: the
        # check-in also expires the account tokens, host-only, with these
        # paths.  It does not expire Tango-ST.
        ('Set-Cookie', f'Tango-WST=; Path=/; {_EPOCH}'),
        ('Set-Cookie', f'Tango-SD=; Path=/; {_EPOCH}'),
        ('Set-Cookie', f'Tango-RT=; Path=/; {_EPOCH}'),
        *(('Set-Cookie', f'Tango-RT=; Path={path}; {_EPOCH}')
          for path in RT_REFRESH_PATHS),
        ('Content-Type', 'application/json')))


def base_routes() -> dict[tuple[str, str, str], Reply | list[Reply]]:
    """Every request the offline extraction path can make.

    The CDN playlist routes exist because the HLS relay fetches its
    upstream playlists through the downloader, and the CDN host ends in
    ``tango.me`` so those requests arrive here too.
    """
    json_type = (('Content-Type', 'application/json'),)
    m3u8_type = (('Content-Type', 'application/vnd.apple.mpegurl'),)
    return {
        CHECK_IN: check_in_reply(),
        ('GET', 'www.tango.me',
         '/api/stream/token-data/v1/web/visitor'): fixture_reply(
             'token.json', headers=json_type),
        ('POST', 'www.tango.me',
         f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch'): fixture_reply(
             'watch.json', headers=json_type),
        ('GET', 'www.tango.me',
         '/api/visitors/profilealias/api/alias/convert'): fixture_reply(
             'alias_convert.txt', headers=json_type),
        ('GET', 'www.tango.me',
         '/api/showroom-streams/api/public/stream/v1/id'): fixture_reply(
             'stream_id.txt', headers=json_type),
        ('POST', 'www.tango.me',
         '/api/showroom-streams/api/public/web/catalog/v1'): fixture_reply(
             'feed.json', headers=json_type),
        ('GET', 'www.tango.me', f'/stream/{STREAM_ID}'): fixture_reply(
            'profile.html', headers=(('Content-Type', 'text/html; charset=utf-8'),)),
        ('GET', 'www.tango.me', f'/{ALIAS}'): fixture_reply(
            'profile_page.html', headers=(('Content-Type', 'text/html; charset=utf-8'),)),
        ('GET', CDN, f'/v2/{_MASTER_HASH}/master.m3u8'): fixture_reply(
            'master.m3u8', headers=m3u8_type),
        ('GET', CDN, f'/v2/{_HD_HASH}/hd.m3u8'): fixture_reply(
            'hd.m3u8', headers=m3u8_type),
        ('GET', CDN, f'/v2/{_LD_HASH}/ld.m3u8'): fixture_reply(
            'ld.m3u8', headers=m3u8_type),
        # A rendition copied from the browser: the CDN ignores the file name
        # and serves that hash's media playlist for any ``*.m3u8``.
        ('GET', CDN, f'/v2/{LIVE_HASH}/live.m3u8'): fixture_reply(
            'hd.m3u8', headers=m3u8_type),
    }


def cookie_names(header: str) -> set[str]:
    return {part.split('=', 1)[0].strip() for part in header.split(';') if '=' in part}


class FakeRH(RequestHandler):
    """Serves ``self.routes`` for tango.me hosts, falls through otherwise.

    It enforces the request contract measured on 2026-11-21 and answers 401
    where the site would: the token needs the visitor cookie and the site
    Referer, a CDN playlist needs the ``tt``/``ttu``/``tte`` triplet and the
    Referer, and ``watch`` needs ``Accept: application/json``.  The feed
    catalog needs the visitor cookie (assumed, not measured).  ``watch`` is
    not a precondition for a playlist (measured); ``require_watch`` lets a
    test switch that alternative rule on.

    "A tampered ``tt`` gets 401" is a live measurement recorded in
    ``docs/kb/tango-me.md``; this fake does not model it.  It checks only
    that the triplet is present; a test that cares about the values sets
    them itself and asserts them on the recorded request.
    """

    RH_KEY = 'Fake'
    _SUPPORTED_URL_SCHEMES = ('http', 'https')
    _SUPPORTED_PROXY_SCHEMES = ('http', 'socks4', 'socks4a', 'socks5', 'socks5h')
    _SUPPORTED_FEATURES = (Features.NO_PROXY, Features.ALL_PROXY)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.routes = {}
        self.requests = []
        self.require_watch = False
        self.watched = set()

    def _refusal(self, request, key):
        """Why the real site would answer 401, or ``None``."""
        method, hostname, path = key
        cookies = cookie_names(request.headers.get('Cookie') or '')
        referer = request.headers.get('Referer') == 'https://www.tango.me/'
        if key == TOKEN:
            if 'Tango-VT' not in cookies or not referer:
                return 'token without the visitor cookie or Referer'
        elif path.endswith('.m3u8'):
            if not {'tt', 'ttu', 'tte'} <= cookies or not referer:
                return 'playlist without the token triplet or Referer'
            # Every CDN fixture belongs to STREAM_ID.
            if self.require_watch and STREAM_ID not in self.watched:
                return 'playlist before watch'
        elif path.endswith('/watch'):
            if request.headers.get('Accept') != 'application/json':
                return 'watch without Accept: application/json'
        elif path.endswith('/catalog/v1'):
            # Not measured; makes the feed's session step load-bearing.
            if 'Tango-VT' not in cookies:
                return 'catalog without the visitor cookie'
        return None

    def _check_extensions(self, extensions):
        # Same extensions UrllibRH accepts, so requests routed at the fake
        # transport (e.g. the relay's `timeout` extension) are supported.
        super()._check_extensions(extensions)
        extensions.pop('cookiejar', None)
        extensions.pop('timeout', None)
        extensions.pop('legacy_ssl', None)

    @wrap_request_errors
    def validate(self, request):
        super().validate(request)
        hostname = urllib.parse.urlparse(request.url).hostname
        if not hostname or not hostname.endswith('tango.me'):
            raise UnsupportedRequest('not a tango host')

    def _send(self, request):
        self.requests.append(request)
        hostname = urllib.parse.urlparse(request.url).hostname

        # Behave like a real handler: send the jar's cookies when the caller
        # did not set its own Cookie header.
        if 'Cookie' not in request.headers:
            cookie_header = self._get_cookiejar(request).get_cookie_header(request.url)
            if cookie_header:
                request.headers['Cookie'] = cookie_header

        key = (request.method, hostname, urllib.parse.urlparse(request.url).path)
        entry = self.routes.get(key)
        if entry is None:
            raise RequestError(f'unrouted request: {request.method} {request.url}')
        refusal = self._refusal(request, key)
        if refusal:
            raise HTTPError(Response(
                io.BytesIO(refusal.encode()), request.url,
                email.message.Message(), 401))
        if isinstance(entry, list):
            reply = entry.pop(0) if len(entry) > 1 else entry[0]
        else:
            reply = entry

        headers = email.message.Message()
        for name, value in reply.headers:
            headers.add_header(name, value)
        response = Response(io.BytesIO(reply.body), request.url, headers, reply.status)

        self._get_cookiejar(request).extract_cookies(
            types.SimpleNamespace(info=lambda: response.headers),
            urllib.request.Request(request.url))

        if reply.status >= 400:
            raise HTTPError(response)
        if key[2].endswith('/watch'):
            self.watched.add(key[2].split('/')[-2])
        return response


def make_downloader(routes, params: dict | None = None) -> YoutubeDL:
    """A downloader whose tango.me traffic is served by ``routes``."""
    ydl = YoutubeDL({'quiet': True, 'skip_download': True, **(params or {})})
    ydl._request_director = ydl.build_request_director([FakeRH, UrllibRH])
    fake = ydl._request_director.handlers['Fake']
    fake.routes = routes
    ydl.fake = fake
    return ydl


def requests_to(ydl, path_fragment: str) -> list[Request]:
    return [r for r in ydl.fake.requests if path_fragment in r.url]
