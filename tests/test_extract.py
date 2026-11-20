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

from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import ExtractorError, UserNotLive

from yt_dlp_plugins.extractor.tango import (
    TangoFeedIE,
    TangoHlsRelay,
    TangoIE,
    _exempt_loopback_from_proxy,
)

from tests import fake_net
from tests.fake_net import (
    ACCOUNT_ID,
    ALIAS,
    STREAM_ID,
    Reply,
    base_routes,
    fixture,
    fixture_reply,
    make_downloader,
    requests_to,
)

WATCH_ERRORS = {
    'watch_wrong_id.json': 'No tango.me stream has that id',
    'watch_terminated.json': 'The stream has ended',
    'watch_bad_request.json': 'audio rooms are the usual cause',
}


def _cookie(domain, name, value='x'):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith('.'),
        domain_initial_dot=domain.startswith('.'), path='/',
        path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False)


def _jwt(exp):
    def enc(part):
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return f'{enc({"typ": "JWT"})}.{enc({"exp": exp})}.sig'


def account_cookie(name):
    """A logged-in session cookie, stored on the account domain."""
    return _cookie('.www.tango.me', name)


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
