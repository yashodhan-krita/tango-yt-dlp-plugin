"""Offline tests for tango.me URL routing.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import unittest

from yt_dlp_plugins.extractor.tango import TangoFeedIE, TangoIE

STREAM = 'jBLKPPFpEsXV2S2C7nC39A'


class StreamUrlTest(unittest.TestCase):
    def test_accepts_every_stream_and_alias_form(self):
        for url in (
            f'https://www.tango.me/stream/{STREAM}',
            f'https://www.tango.me/live/{STREAM}',
            'https://www.tango.me/wisdomhood',
            'https://www.tango.me/wisdomhood/',
            'https://www.tango.me/profile/wisdomhood',
            'https://www.tango.me/wisdomhood?tab=about',
            'https://m.tango.me/wisdomhood',
            'https://www.tango.me/@wisdomhood',
        ):
            match = TangoIE._match_valid_url(url)
            self.assertTrue(match, url)
            self.assertEqual(match.group('id'), STREAM if STREAM in url
                             else '@wisdomhood' if '@' in url else 'wisdomhood', url)

    def test_reserved_routes_are_not_aliases(self):
        for url in ('https://www.tango.me/stream/',
                    'https://www.tango.me/profile/',
                    'https://www.tango.me/search',
                    'https://www.tango.me/live'):
            self.assertFalse(TangoIE.suitable(url), url)


class FeedUrlTest(unittest.TestCase):
    def test_owns_the_live_index_only(self):
        for url in ('https://www.tango.me/live',
                    'https://www.tango.me/live/',
                    'https://www.tango.me/live?x=1'):
            self.assertTrue(TangoFeedIE.suitable(url), url)
        self.assertFalse(TangoFeedIE.suitable(f'https://www.tango.me/live/{STREAM}'))


if __name__ == '__main__':
    unittest.main()
