"""Offline tests for the tango extractor, driven by captured fixtures.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import unittest

from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import ExtractorError

from yt_dlp_plugins.extractor.tango import TangoIE

from tests.fake_net import (
    STREAM_ID,
    Reply,
    base_routes,
    fixture_reply,
    make_downloader,
    requests_to,
)

WATCH_ERRORS = {
    'watch_wrong_id.json': 'No tango.me stream has that id',
    'watch_terminated.json': 'The stream has ended',
    'watch_bad_request.json': 'audio rooms are the usual cause',
}


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


if __name__ == '__main__':
    unittest.main()
