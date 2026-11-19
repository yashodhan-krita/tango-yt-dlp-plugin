"""Offline tests for the tango extractor, driven by captured fixtures.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import unittest

from yt_dlp.networking.exceptions import RequestError

from yt_dlp_plugins.extractor.tango import TangoIE

from tests.fake_net import STREAM_ID, base_routes, make_downloader


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


if __name__ == '__main__':
    unittest.main()
