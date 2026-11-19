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

_ACCOUNT_ID = 'GGGGHHHHIIIIJJJJKKKKLL'
_MASTER_HASH = '1' * 32
_HD_HASH = '2' * 32
_LD_HASH = '3' * 32

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


def base_routes() -> dict[tuple[str, str, str], Reply | list[Reply]]:
    """Every request the offline extraction path can make.

    The three CDN playlist routes exist because the HLS relay fetches its
    upstream playlists through the downloader, and the CDN host ends in
    ``tango.me`` so those requests arrive here too.
    """
    json_type = (('Content-Type', 'application/json'),)
    m3u8_type = (('Content-Type', 'application/vnd.apple.mpegurl'),)
    return {
        ('POST', 'www.tango.me',
         '/api/visitor-lobby/api/public/visitors/v1/check-in'): fixture_reply(
             'check_in.json', headers=(
                 # Host-only cookies, like the real check-in response: the
                 # visitor identity lands on ``www.tango.me`` (no leading dot),
                 # separate from the account session on ``.www.tango.me``.
                 ('Set-Cookie', 'Tango-VT=visitor-token; Path=/'),
                 ('Set-Cookie', 'Tango-SD=visitor-session; Path=/'),
                 ('Content-Type', 'application/json'))),
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
        ('GET', CDN, f'/v2/{_MASTER_HASH}/master.m3u8'): fixture_reply(
            'master.m3u8', headers=m3u8_type),
        ('GET', CDN, f'/v2/{_HD_HASH}/hd.m3u8'): fixture_reply(
            'hd.m3u8', headers=m3u8_type),
        ('GET', CDN, f'/v2/{_LD_HASH}/ld.m3u8'): fixture_reply(
            'ld.m3u8', headers=m3u8_type),
    }


class FakeRH(RequestHandler):
    """Serves ``self.routes`` for tango.me hosts, falls through otherwise."""

    RH_KEY = 'Fake'
    _SUPPORTED_URL_SCHEMES = ('http', 'https')
    _SUPPORTED_PROXY_SCHEMES = ('http', 'socks4', 'socks4a', 'socks5', 'socks5h')
    _SUPPORTED_FEATURES = (Features.NO_PROXY, Features.ALL_PROXY)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.routes = {}
        self.requests = []

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
