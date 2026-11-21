"""Offline tests for the tango extractor, driven by captured fixtures.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import base64
import http.cookiejar
import json
import os
import time
import unittest
from unittest import mock
from urllib.parse import urlparse

from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.external import FFmpegFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.utils import ExtractorError, UserNotLive
from yt_dlp.utils.networking import select_proxy

from yt_dlp_plugins.extractor.tango import (
    TangoCdnIE,
    TangoFeedIE,
    TangoHlsRelay,
    TangoIE,
    _exempt_loopback_from_proxy,
)

from tests import fake_net
from tests.fake_net import (
    ACCOUNT_ID,
    ALIAS,
    CHECK_IN,
    LIVE_HASH,
    RT_REFRESH_PATHS,
    STREAM_ID,
    TOKEN,
    WATCH,
    Reply,
    base_routes,
    check_in_reply,
    cookie_names,
    fixture,
    fixture_reply,
    make_downloader,
    requests_to,
)

JSON_TYPE = (('Content-Type', 'application/json'),)
STREAM_URL = f'https://www.tango.me/stream/{STREAM_ID}'

# A logged-in Chrome profile, as measured: Tango-ST and Tango-RT host-only
# (Tango-RT also at the two refresh paths), Tango-WST and the account
# Tango-SD on ``.www.tango.me``.
ACCOUNT_KEYS = frozenset({
    ('www.tango.me', '/', 'Tango-ST'),
    ('www.tango.me', '/', 'Tango-RT'),
    *(('www.tango.me', path, 'Tango-RT') for path in RT_REFRESH_PATHS),
    ('.www.tango.me', '/', 'Tango-WST'),
    ('.www.tango.me', '/', 'Tango-SD'),
})


def seed_account(ydl):
    # The value marks them, so a Cookie header can be checked for leaks.
    for domain, path, name in ACCOUNT_KEYS:
        ydl.cookiejar.set_cookie(_cookie(domain, name, 'account', path))


def jar_keys(ydl):
    return {(c.domain, c.path, c.name) for c in ydl.cookiejar}

WATCH_ERRORS = {
    'watch_wrong_id.json': 'No tango.me stream has that id',
    'watch_terminated.json': 'The stream has ended',
    'watch_bad_request.json': 'audio rooms are the usual cause',
}


def _cookie(domain, name, value='x', path='/'):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith('.'),
        domain_initial_dot=domain.startswith('.'), path=path,
        path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False)


def _jwt(exp):
    def enc(part):
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return f'{enc({"typ": "JWT"})}.{enc({"exp": exp})}.sig'


def visitor_cookie(exp):
    """An unexpired ``Tango-VT`` visitor JWT, stored on the visitor domain."""
    return _cookie('www.tango.me', 'Tango-VT', _jwt(exp))


class ExtractionTest(unittest.TestCase):
    def test_check_in_stores_the_visitor_token(self):
        ydl = make_downloader(base_routes())
        TangoIE(ydl)._ensure_session(STREAM_ID)
        self.assertIn('Tango-VT', {c.name for c in ydl.cookiejar})

    def test_an_unrouted_request_is_loud(self):
        ydl = make_downloader({})
        with self.assertRaises(RequestError) as ctx:
            TangoIE(ydl)._api_json('/nope', STREAM_ID, note=False)
        self.assertIn('unrouted', str(ctx.exception))

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
            TangoIE(make_downloader(routes, params={'no_warnings': True}))._watch(STREAM_ID)
        self.assertFalse(ctx.exception.expected)

    def test_an_empty_alias_body_is_not_retried(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me', '/api/visitors/profilealias/api/alias/convert')] = Reply(b'')
        ydl = make_downloader(routes)
        with self.assertRaises(ExtractorError) as ctx:
            TangoIE(ydl)._resolve_stream_id('nobody')
        self.assertIn('No tango.me profile found', str(ctx.exception))
        self.assertEqual(len(requests_to(ydl, '/alias/convert')), 1)

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

    def test_a_401_with_a_jar_visitor_re_registers_once(self):
        # A Tango-VT from a cookie file can be unexpired yet refused.
        routes = base_routes()
        routes[WATCH] = [Reply(b'', status=401),
                         fixture_reply('watch.json', headers=JSON_TYPE)]
        ydl = make_downloader(routes, {'no_warnings': True})
        ydl.cookiejar.set_cookie(visitor_cookie(time.time() + 3600))
        info = TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertTrue(info['formats'])
        self.assertEqual(len(requests_to(ydl, 'check-in')), 1)
        self.assertEqual(len(requests_to(ydl, '/watch')), 2)

    def test_a_refused_watch_is_not_retried_beyond_the_re_registration(self):
        routes = base_routes()
        routes[WATCH] = Reply(b'', status=401)
        ydl = make_downloader(routes, {'no_warnings': True})
        ydl.cookiejar.set_cookie(visitor_cookie(time.time() + 3600))
        with self.assertRaises(ExtractorError) as ctx:
            TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertIn('--cookies-from-browser', str(ctx.exception))
        self.assertEqual(len(requests_to(ydl, '/watch')), 2)

    def test_an_issued_visitor_is_not_re_registered_on_a_401(self):
        routes = base_routes()
        routes[WATCH] = Reply(b'', status=401)
        ydl = make_downloader(routes, {'no_warnings': True})
        with self.assertRaises(ExtractorError):
            TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertEqual(len(requests_to(ydl, 'check-in')), 1)
        self.assertEqual(len(requests_to(ydl, '/watch')), 1)

    def test_an_unreadable_jar_visitor_is_replaced(self):
        ydl = make_downloader(base_routes())
        ydl.cookiejar.set_cookie(_cookie('www.tango.me', 'Tango-VT', 'h.W10.sig'))
        TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertEqual(len(requests_to(ydl, 'check-in')), 1)

    def test_the_token_username_is_quoted_in_the_master_url(self):
        routes = base_routes()
        routes[TOKEN] = Reply(json.dumps({
            'token': 't', 'username': 'a&b=c d#e', 'expireAt': 4102444800,
        }).encode(), headers=JSON_TYPE)
        ydl = make_downloader(routes)
        TangoIE(ydl)._real_extract(STREAM_URL)
        master = requests_to(ydl, 'master.m3u8')[0]
        self.assertIn('username=a%26b%3Dc%20d%23e', master.url)
        # The cookie keeps the value as the site sent it.
        self.assertIn('ttu=a&b=c d#e', master.headers['Cookie'])

    def test_a_capitalised_stream_route_is_a_stream(self):
        ydl = make_downloader(base_routes())
        info = TangoIE(ydl)._real_extract(f'https://www.tango.me/Stream/{STREAM_ID}')
        self.assertEqual(info['id'], STREAM_ID)
        self.assertEqual(requests_to(ydl, 'alias/convert'), [])

    def test_a_non_string_code_still_explains_itself(self):
        routes = base_routes()
        doc = json.loads(fixture('watch.json'))
        doc['code'] = {'FAILED': True}
        routes[('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = Reply(
            json.dumps(doc).encode(),
            headers=(('Content-Type', 'application/json'),))
        with self.assertRaises(ExtractorError) as ctx:
            TangoIE(make_downloader(routes))._watch(STREAM_ID)
        self.assertIn('tango.me returned', str(ctx.exception))
        self.assertTrue(ctx.exception.expected)

    def test_the_relay_is_closed_when_the_manifest_cannot_be_read(self):
        routes = base_routes()
        routes[('GET', fake_net.CDN,
                f'/v2/{fake_net._MASTER_HASH}/master.m3u8')] = Reply(
            b'not a playlist',
            headers=(('Content-Type', 'text/plain'),))

        # Patch the class the extractor actually calls, not the module path:
        # yt-dlp's plugin loader can replace the module object in sys.modules,
        # which leaves a string patch target pointing at a different class.
        original_close = TangoHlsRelay.close
        closed = []

        def close(relay):
            closed.append(relay)
            original_close(relay)

        with mock.patch.object(TangoHlsRelay, 'close', close):
            with self.assertRaises(ExtractorError) as ctx:
                TangoIE(make_downloader(routes))._real_extract(
                    f'https://www.tango.me/stream/{STREAM_ID}')
        self.assertIn('manifest could not be read', str(ctx.exception))
        self.assertTrue(ctx.exception.expected)
        self.assertEqual(len(closed), 1)

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

    def test_a_copied_cdn_playlist_is_relayed_with_a_fresh_token(self):
        # The link the upstream issue documents: a rendition URL with the
        # browser's expired token and username still attached.
        path = f'/v2/{LIVE_HASH}/live.m3u8'
        ydl = make_downloader(base_routes())
        info = TangoCdnIE(ydl)._real_extract(
            f'https://{fake_net.CDN}{path}?token=stale&username=stale')
        self.assertEqual(info['id'], LIVE_HASH)
        self.assertIs(info['is_live'], True)
        self.assertTrue(info['formats'])
        # A media playlist parses to one format whose url is the playlist
        # itself; yt-dlp sets no manifest_url for that shape.
        manifest = info['formats'][0].get('manifest_url') or info['formats'][0]['url']
        self.assertEqual(urlparse(manifest).hostname, '127.0.0.1')
        self.assertEqual(urlparse(manifest).path, path)
        # The copied path is relayed unchanged; only the stale query is gone.
        upstream = [urlparse(r.url) for r in requests_to(ydl, f'/v2/{LIVE_HASH}/')]
        self.assertTrue(upstream)
        self.assertEqual({(u.path, u.query) for u in upstream}, {(path, '')})
        self.assertEqual(info['webpage_url'], f'https://{fake_net.CDN}{path}')
        # Nothing in the URL names a broadcaster and the token endpoint is not
        # stream-scoped, so no identity call is made.
        self.assertEqual(requests_to(ydl, 'alias/convert'), [])
        self.assertEqual(requests_to(ydl, '/watch'), [])

    def test_a_copied_cdn_url_is_authorised_by_the_triplet_alone(self):
        # Measured 2026-11-21: check-in, token, then the playlist answers 200
        # without `watch`.  The fake answers 401 to a playlist without the
        # triplet and to a token call without the visitor cookie.
        ydl = make_downloader(base_routes())
        info = TangoCdnIE(ydl)._real_extract(
            f'https://{fake_net.CDN}/v2/{LIVE_HASH}/live.m3u8?token=stale&username=stale')
        urls = [r.url for r in ydl.fake.requests]
        check_in = next(i for i, u in enumerate(urls) if 'check-in' in u)
        tokens = [i for i, u in enumerate(urls) if 'token-data' in u]
        # The session step ran first, so the one token call needed no recovery.
        self.assertEqual(len(tokens), 1)
        self.assertLess(check_in, tokens[0])
        cdn = requests_to(ydl, f'/v2/{LIVE_HASH}/')
        self.assertTrue(cdn)
        for request in cdn:
            self.assertLessEqual({'tt', 'ttu', 'tte'},
                                 cookie_names(request.headers['Cookie']))
            self.assertNotIn('token=', request.url)
            self.assertNotIn('username=', request.url)
        self.assertEqual(requests_to(ydl, '/watch'), [])
        for field in ('webpage_url', 'title'):
            self.assertNotIn('?', info[field])
            self.assertNotIn('stale', info[field])

    def test_the_fake_cdn_answers_like_the_measured_one(self):
        ydl = make_downloader(base_routes())
        url = f'https://{fake_net.CDN}/v2/{LIVE_HASH}/live.m3u8'
        site = {'Referer': 'https://www.tango.me/'}
        fresh = {**site, 'Cookie': 'tt=t; ttu=u; tte=1'}

        def status(headers):
            try:
                return ydl.urlopen(Request(url, headers=headers)).status
            except HTTPError as err:
                return err.status

        self.assertEqual(status(site), 401)
        self.assertEqual(status({'Cookie': fresh['Cookie']}), 401)
        self.assertEqual(status(fresh), 200)
        # The alternative rule, measured not to be the site's, can be enforced.
        ydl.fake.require_watch = True
        self.assertEqual(status(fresh), 401)
        TangoIE(ydl)._watch(STREAM_ID)
        self.assertEqual(status(fresh), 200)

    def test_a_live_stream_carries_its_start_time(self):
        info = TangoIE(make_downloader(base_routes()))._real_extract(
            f'https://www.tango.me/stream/{STREAM_ID}')
        self.assertAlmostEqual(info['timestamp'], int(time.time()) - 4885, delta=5)

    def test_only_a_timed_live_stream_carries_a_start_time(self):
        for overrides in ({'status': 'ENDED'}, {'duration': None}):
            routes = base_routes()
            doc = json.loads(fixture('watch.json'))
            doc['details']['stream'].update(overrides)
            routes[('POST', 'www.tango.me', f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = Reply(
                json.dumps(doc).encode(),
                headers=(('Content-Type', 'application/json'),))
            with self.subTest(overrides):
                info = TangoIE(make_downloader(
                    routes, params={'no_warnings': True}))._real_extract(
                    f'https://www.tango.me/stream/{STREAM_ID}')
                self.assertNotIn('timestamp', info)

    def test_alias_and_at_alias_reach_the_same_stream(self):
        for entry in (ALIAS, f'@{ALIAS}'):
            info = TangoIE(make_downloader(base_routes()))._real_extract(
                f'https://www.tango.me/{entry}')
            self.assertEqual(info['id'], STREAM_ID)

    def test_a_profile_url_reads_the_profile_page(self):
        # The stream page fixture says "Tango Tester"; the profile page says
        # "Tango Tester Two", so the richer profile source is proven to be used.
        info = TangoIE(make_downloader(base_routes()))._real_extract(
            f'https://www.tango.me/@{ALIAS}')
        self.assertEqual(info['id'], STREAM_ID)
        self.assertEqual(info['uploader'], 'Tango Tester Two')
        self.assertEqual(info['uploader_id'], ALIAS)
        self.assertEqual(info['channel_url'], f'https://www.tango.me/{ALIAS}')
        self.assertEqual(info['channel_follower_count'], 1234)
        self.assertEqual(
            info['description'],
            'Watch Tango Tester Two on Tango. Send gifts, join the chat and '
            'connect in real time.')

    def test_a_profile_account_id_is_never_read_as_a_stream_id(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me',
                '/api/visitors/profilealias/api/alias/convert')] = Reply(
                    b'', status=404)
        routes[('GET', 'www.tango.me', f'/{ACCOUNT_ID}')] = fixture_reply(
            'profile_page_account.html',
            headers=(('Content-Type', 'text/html; charset=utf-8'),))
        ydl = make_downloader(routes)
        info = TangoIE(ydl)._real_extract(
            f'https://www.tango.me/profile/{ACCOUNT_ID}')
        self.assertEqual(info['id'], STREAM_ID)
        self.assertEqual(info['channel_id'], ACCOUNT_ID)
        self.assertEqual(info['channel_follower_count'], 1234)
        lookups = requests_to(ydl, '/stream/v1/id')
        self.assertEqual(len(lookups), 1)
        self.assertIn(f'broadcasterId={ACCOUNT_ID}', lookups[0].url)
        self.assertEqual(requests_to(ydl, f'stream/{ACCOUNT_ID}'), [])

    def test_an_offline_profile_explains_how_to_wait(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me',
                '/api/showroom-streams/api/public/stream/v1/id')] = Reply(
                    b'', status=404)
        with self.assertRaises(UserNotLive) as ctx:
            TangoIE(make_downloader(routes))._real_extract(
                f'https://www.tango.me/@{ALIAS}')
        self.assertIn(ALIAS, str(ctx.exception))
        self.assertIn('--wait-for-video', str(ctx.exception))

    def test_the_wait_hint_is_not_repeated_when_already_waiting(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me',
                '/api/showroom-streams/api/public/stream/v1/id')] = Reply(
                    b'', status=404)
        with self.assertRaises(UserNotLive) as ctx:
            TangoIE(make_downloader(
                routes, {'wait_for_video': (15, None)}))._real_extract(
                    f'https://www.tango.me/@{ALIAS}')
        self.assertNotIn('--wait-for-video', str(ctx.exception))

    def test_a_404_error_page_is_not_taken_as_an_id(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me',
                '/api/visitors/profilealias/api/alias/convert')] = Reply(
                    b'<html>bad gateway</html>')
        with self.assertRaises(ExtractorError) as ctx:
            TangoIE(make_downloader(routes))._resolve_stream_id('nobody')
        self.assertIn('No tango.me profile found', str(ctx.exception))

    def test_the_noscript_page_is_named_without_a_request(self):
        for path in ('stream/noscript.html', 'live/noscript.html',
                     'noscript.html'):
            ydl = make_downloader(base_routes())
            with self.subTest(path), self.assertRaises(ExtractorError) as ctx:
                TangoIE(ydl)._real_extract(f'https://www.tango.me/{path}')
            self.assertIn('noscript.html', str(ctx.exception))
            self.assertIn('https://www.tango.me/stream/', str(ctx.exception))
            self.assertIs(ctx.exception.expected, True)
            self.assertEqual(ydl.fake.requests, [])

    def test_a_percent_encoded_alias_is_decoded(self):
        info = TangoIE(make_downloader(base_routes()))._real_extract(
            f'https://www.tango.me/%40{ALIAS}')
        self.assertEqual(info['id'], STREAM_ID)
        self.assertEqual(info['uploader_id'], ALIAS)

    def test_a_terminated_profile_waits_instead_of_downloading(self):
        routes = base_routes()
        routes[('POST', 'www.tango.me',
                f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = (
                    fixture_reply('watch_terminated.json'))
        with self.assertRaises(UserNotLive) as ctx:
            TangoIE(make_downloader(routes))._real_extract(
                f'https://www.tango.me/@{ALIAS}')
        self.assertIn(ALIAS, str(ctx.exception))

    def test_an_ended_profile_waits_instead_of_downloading(self):
        routes = base_routes()
        doc = json.loads(fixture('watch.json'))
        doc['details']['stream']['status'] = 'ENDED'
        routes[('POST', 'www.tango.me',
                f'/api/visitors/stream/client/v2/stream/{STREAM_ID}/watch')] = Reply(
                    json.dumps(doc).encode(),
                    headers=(('Content-Type', 'application/json'),))
        with self.assertRaises(UserNotLive) as ctx:
            TangoIE(make_downloader(routes))._real_extract(
                f'https://www.tango.me/@{ALIAS}')
        self.assertIn(ALIAS, str(ctx.exception))

    def test_a_graph_profile_page_and_a_zero_follower_count(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me', f'/{ALIAS}')] = Reply(
            b'<script type="application/ld+json">{"@graph": [{"@type": '
            b'"ProfilePage", "mainEntity": {"@type": "Person", "name": '
            b'"Zero Follower", "identifier": "tango_tester", '
            b'"interactionStatistic": [{"interactionType": '
            b'"https://schema.org/FollowAction", "userInteractionCount": '
            b'"0"}]}}]}</script>',
            headers=(('Content-Type', 'text/html; charset=utf-8'),))
        info = TangoIE(make_downloader(routes))._real_extract(
            f'https://www.tango.me/@{ALIAS}')
        self.assertEqual(info['uploader'], 'Zero Follower')
        self.assertEqual(info['channel_follower_count'], 0)

    def test_a_persistent_token_failure_is_loud(self):
        routes = base_routes()
        routes[('GET', 'www.tango.me',
                '/api/stream/token-data/v1/web/visitor')] = Reply(
                    b'busy', status=503)
        with self.assertRaises(ExtractorError) as ctx:
            TangoIE(make_downloader(
                routes, {'no_warnings': True}))._real_extract(
                    f'https://www.tango.me/stream/{STREAM_ID}')
        self.assertIn('did not issue a CDN token', str(ctx.exception))

    def test_without_the_restore_the_check_in_logs_the_account_out(self):
        # Negative control for the test below: the fake reply really removes
        # what the restore puts back.
        ydl = make_downloader(base_routes())
        seed_account(ydl)
        ie = TangoIE(ydl)
        with mock.patch.object(TangoIE, '_restore_account_cookies',
                               lambda self, snapshot: None):
            ie._ensure_session(STREAM_ID)
            ie._watch(STREAM_ID)
        keys = jar_keys(ydl)
        for path in ('/', *RT_REFRESH_PATHS):
            self.assertNotIn(('www.tango.me', path, 'Tango-RT'), keys)
        # The measured reply does not expire Tango-ST, and it expires only
        # host-only copies, so the `.www.` cookies are never touched.
        self.assertIn(('www.tango.me', '/', 'Tango-ST'), keys)
        self.assertIn(('.www.tango.me', '/', 'Tango-WST'), keys)
        self.assertIn(('.www.tango.me', '/', 'Tango-SD'), keys)
        watch = cookie_names(requests_to(ydl, '/watch')[0].headers['Cookie'])
        self.assertNotIn('Tango-RT', watch)

    def test_the_account_session_survives_the_check_in(self):
        # The check-in reply expires the account tokens; a logged-in run must
        # still reach `watch` with them.
        ydl = make_downloader(base_routes())
        seed_account(ydl)
        TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertEqual(len(requests_to(ydl, 'check-in')), 1)
        # The `.www.` Tango-WST / Tango-SD snapshot is precautionary: the
        # measured reply only expires host-only copies.
        self.assertLessEqual(ACCOUNT_KEYS, jar_keys(ydl))
        self.assertIn(('www.tango.me', '/', 'Tango-VT'), jar_keys(ydl))
        watch = requests_to(ydl, '/watch')[0].headers['Cookie']
        self.assertIn('Tango-ST=', watch)
        self.assertIn('Tango-RT=', watch)

    def test_token_calls_carry_the_account_cookies(self):
        # The jar is not edited around a token call, so an account-bound
        # token can be issued.
        ydl = make_downloader(base_routes())
        seed_account(ydl)
        ie = TangoIE(ydl)
        ie._ensure_session(STREAM_ID)
        ie._issue_token(STREAM_ID)
        cookie = requests_to(ydl, 'token-data')[0].headers['Cookie']
        self.assertIn('Tango-VT=', cookie)
        self.assertLessEqual({'Tango-ST', 'Tango-RT', 'Tango-WST'},
                             cookie_names(cookie))
        self.assertIn('Tango-SD=account', cookie)

    def test_the_jar_never_lacks_the_account_during_an_extraction(self):
        # A --cookies file is saved from this jar whenever yt-dlp stops, and
        # a relay thread may be mid-refresh then.  The token route throttles
        # once, so the forced check-in inside the token cycle runs too.  The
        # jar is checked before every request, so the requests after each
        # check-in prove its `finally` restored the account.  The only gap is
        # between a check-in reply and that `finally`; no request runs there.
        routes = base_routes()
        routes[TOKEN] = [Reply(b'', status=403),
                         fixture_reply('token.json', headers=JSON_TYPE)]
        ydl = make_downloader(routes, {'no_warnings': True})
        seed_account(ydl)
        seen = []
        send = ydl.fake._send

        def spy(request):
            seen.append((request.url, jar_keys(ydl)))
            return send(request)

        ydl.fake._send = spy
        with mock.patch('time.sleep'):
            TangoIE(ydl)._real_extract(STREAM_URL)
        urls = [url for url, _ in seen]
        self.assertEqual(sum('check-in' in url for url in urls), 2)
        self.assertGreaterEqual(sum('token-data' in url for url in urls), 2)
        last_check_in = max(i for i, url in enumerate(urls) if 'check-in' in url)
        self.assertGreater(len(urls), last_check_in + 1)
        for url, keys in seen:
            self.assertLessEqual(ACCOUNT_KEYS, keys, url)
        self.assertLessEqual(ACCOUNT_KEYS, jar_keys(ydl))

    def test_a_second_run_on_the_same_instance_can_self_heal(self):
        # yt-dlp reuses the extractor, and --wait-for-video re-extracts on it.
        routes = base_routes()
        routes[CHECK_IN] = check_in_reply(_jwt(time.time() + 3600))
        ok = fixture_reply('watch.json', headers=JSON_TYPE)
        routes[WATCH] = [ok, Reply(b'', status=401), ok]
        ydl = make_downloader(routes, {'no_warnings': True})
        ie = TangoIE(ydl)
        ie._real_extract(STREAM_URL)
        self.assertEqual(len(requests_to(ydl, 'check-in')), 1)
        info = ie._real_extract(STREAM_URL)       # reuses the jar visitor
        self.assertTrue(info['formats'])
        self.assertEqual(len(requests_to(ydl, 'check-in')), 2)
        self.assertEqual(len(requests_to(ydl, '/watch')), 3)

    def test_a_throttled_token_recovers_with_a_fresh_visitor(self):
        routes = base_routes()
        routes[CHECK_IN] = [check_in_reply('visitor-one'),
                            check_in_reply('visitor-two')]
        routes[TOKEN] = [Reply(b'', status=403),
                         fixture_reply('token.json', headers=JSON_TYPE)]
        ydl = make_downloader(routes, {'no_warnings': True})
        seed_account(ydl)
        ie = TangoIE(ydl)
        ie._ensure_session(STREAM_ID)
        with mock.patch('time.sleep'):
            data = ie._issue_token(STREAM_ID)
        self.assertEqual(data['token'], 'test-token')
        check_ins = requests_to(ydl, 'check-in')
        self.assertEqual(len(check_ins), 2)
        # The throttled identity is forgotten before re-registering.
        self.assertNotIn('Tango-VT', cookie_names(
            check_ins[1].headers.get('Cookie') or ''))
        self.assertEqual(
            [c.value for c in ydl.cookiejar if c.name == 'Tango-VT'],
            ['visitor-two'])
        tokens = requests_to(ydl, 'token-data')
        self.assertEqual(len(tokens), 2)
        self.assertIn('Tango-VT=visitor-two', tokens[1].headers['Cookie'])
        self.assertIn('Tango-ST=account', tokens[1].headers['Cookie'])
        self.assertLessEqual(ACCOUNT_KEYS, jar_keys(ydl))

    def test_datadome_from_a_token_reply_rides_on_the_next_token_call(self):
        """Pins a known gap (F19), not a wish.

        ``_drop_bot_cookies`` runs only in ``_ensure_session``, so a
        ``datadome`` cookie that a token reply sets is sent on the next token
        call and is dropped only at the next session step.
        """
        routes = base_routes()
        routes[TOKEN] = fixture_reply('token.json', headers=(
            *JSON_TYPE, ('Set-Cookie', 'datadome=bot; Domain=.tango.me; Path=/')))
        ydl = make_downloader(routes)
        ie = TangoIE(ydl)
        ie._ensure_session(STREAM_ID)
        ie._issue_token(STREAM_ID)
        ie._issue_token(STREAM_ID)
        second = requests_to(ydl, 'token-data')[1].headers['Cookie']
        self.assertIn('datadome', cookie_names(second))
        ie._ensure_session(STREAM_ID)
        self.assertNotIn('datadome', {c.name for c in ydl.cookiejar})

    def test_a_failed_check_in_keeps_the_account_cookies(self):
        for body in (b'[]', b'not json'):
            routes = base_routes()
            routes[CHECK_IN] = check_in_reply(body=body)
            ydl = make_downloader(routes, {'no_warnings': True})
            seed_account(ydl)
            with self.subTest(body), self.assertRaises(ExtractorError):
                TangoIE(ydl)._check_in(STREAM_ID)
            self.assertLessEqual(ACCOUNT_KEYS, jar_keys(ydl))

    def test_formats_carry_the_site_origin_and_referer(self):
        info = TangoIE(make_downloader(base_routes()))._real_extract(STREAM_URL)
        for f in info['formats']:
            self.assertEqual(f['http_headers']['Origin'], 'https://www.tango.me')
            self.assertEqual(f['http_headers']['Referer'], 'https://www.tango.me/')

    def test_extraction_exempts_loopback_from_the_proxy(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('no_proxy', None)
            os.environ.pop('NO_PROXY', None)
            TangoIE(make_downloader(base_routes()))._real_extract(STREAM_URL)
            for name in ('no_proxy', 'NO_PROXY'):
                self.assertLessEqual({'127.0.0.1', 'localhost'},
                                     set(os.environ[name].split(',')))

    def test_an_anonymous_run_gains_no_account_cookies(self):
        ydl = make_downloader(base_routes())
        TangoIE(ydl)._real_extract(STREAM_URL)
        # The strict fake let it through: it reached the token and the CDN.
        self.assertTrue(requests_to(ydl, 'token-data'))
        self.assertTrue(requests_to(ydl, '.m3u8'))
        names = {(c.domain, c.name) for c in ydl.cookiejar}
        self.assertFalse(
            {n for _, n in names} & {'Tango-ST', 'Tango-RT', 'Tango-WST'}, names)
        self.assertNotIn(('.www.tango.me', 'Tango-SD'), names)
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

    def test_a_missing_alias_profile_page_keeps_the_alias_as_the_name(self):
        # An alias URL fetches only the profile page, never the stream page.
        routes = base_routes()
        routes[('GET', 'www.tango.me', f'/{ALIAS}')] = Reply(b'', status=404)
        ydl = make_downloader(routes, {'no_warnings': True})
        info = TangoIE(ydl)._real_extract(f'https://www.tango.me/@{ALIAS}')
        self.assertEqual(info['title'], f'{ALIAS} - Tango live')
        self.assertNotIn(f'https://www.tango.me/stream/{STREAM_ID}',
                         [request.url for request in ydl.fake.requests])

    def test_the_feed_lists_the_captured_stream(self):
        feed = TangoFeedIE(make_downloader(base_routes()))._real_extract(
            'https://www.tango.me/live')
        self.assertEqual([entry['id'] for entry in feed['entries']], [STREAM_ID])

    def test_the_feed_needs_its_session_step(self):
        # The fake answers 401 to a catalog call without a visitor cookie, so
        # the feed above passing proves the session step ran first.
        ydl = make_downloader(base_routes())
        with mock.patch.object(TangoFeedIE, '_ensure_session', lambda *a, **k: None), \
                self.assertRaises(ExtractorError) as ctx:
            TangoFeedIE(ydl)._real_extract('https://www.tango.me/live')
        self.assertEqual(getattr(ctx.exception.cause, 'status', None), 401)

    def test_a_stream_extraction_watches_before_the_playlists(self):
        ydl = make_downloader(base_routes())
        ydl.fake.require_watch = True
        info = TangoIE(ydl)._real_extract(STREAM_URL)
        self.assertTrue(info['formats'])
        self.assertTrue(requests_to(ydl, '.m3u8'))

    def test_relayed_formats_are_downloaded_by_ffmpeg(self):
        # ffmpeg honours no_proxy; the native HLS downloader would send the
        # loopback relay through --proxy.
        for ie, url in (
                (TangoIE, STREAM_URL),
                (TangoCdnIE, f'https://{fake_net.CDN}/v2/{LIVE_HASH}/live.m3u8')):
            info = ie(make_downloader(base_routes()))._real_extract(url)
            for f in info['formats']:
                with self.subTest(ie=ie.IE_NAME, format=f['format_id']):
                    self.assertEqual(f['protocol'], 'm3u8')
                    self.assertIs(get_suitable_downloader(dict(f)), FFmpegFD)

    def test_the_proxy_rule_behind_the_ffmpeg_protocol(self):
        # --proxy gives {'all': proxy}; with no 'no' entry, no_proxy is ignored.
        proxy = 'http://proxy.example:3128'
        with mock.patch.dict(os.environ, {'no_proxy': '127.0.0.1,localhost'}):
            self.assertEqual(
                select_proxy('http://127.0.0.1:1234/live.m3u8', {'all': proxy}),
                proxy)
        relay = 'http://127.0.0.1:1234/live.m3u8'
        self.assertIs(get_suitable_downloader(
            {'url': relay, 'protocol': 'm3u8'}), FFmpegFD)
        self.assertIs(get_suitable_downloader(
            {'url': relay, 'protocol': 'm3u8_native'}), HlsFD)

    def test_loopback_is_exempt_from_the_proxy(self):
        with mock.patch.dict(os.environ, {'no_proxy': 'corp.example'}, clear=True):
            _exempt_loopback_from_proxy()
            self.assertEqual(os.environ['no_proxy'], 'corp.example,127.0.0.1,localhost')
            self.assertEqual(os.environ['NO_PROXY'], '127.0.0.1,localhost')
            _exempt_loopback_from_proxy()
            self.assertEqual(os.environ['no_proxy'], 'corp.example,127.0.0.1,localhost')

    def test_the_manifest_fetch_bypasses_a_configured_proxy(self):
        # --proxy gives yt-dlp {'all': ...} with no 'no' entry, so no_proxy is
        # ignored for its own requests; the relay fetch must say so per request.
        ie = TangoIE()
        ie._request_webpage = mock.Mock(return_value=False)
        ie._hls_formats('http://127.0.0.1:1234/live.m3u8?m=x', 'v', live=True)
        request = ie._request_webpage.call_args.args[0]
        self.assertEqual(request.proxies, {'all': '__noproxy__'})


if __name__ == '__main__':
    unittest.main()
