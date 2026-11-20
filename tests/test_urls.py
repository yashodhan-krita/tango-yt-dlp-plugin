"""Offline tests for tango.me URL routing.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import unittest

from yt_dlp_plugins.extractor.tango import TangoFeedIE, TangoIE

from tests.fake_net import ACCOUNT_ID

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

    def test_profile_and_stream_forms_are_told_apart(self):
        account = f'https://www.tango.me/profile/{ACCOUNT_ID}'
        self.assertTrue(TangoIE.suitable(account))
        self.assertEqual(TangoIE._match_valid_url(account).group('id'), ACCOUNT_ID)
        self.assertTrue(TangoIE._is_profile_url(account))
        for url in (f'https://www.tango.me/stream/{ACCOUNT_ID}',
                    f'https://www.tango.me/live/{ACCOUNT_ID}',
                    f'https://m.tango.me/stream/{ACCOUNT_ID}'):
            self.assertFalse(TangoIE._is_profile_url(url), url)
        for url in (f'https://www.tango.me/{ACCOUNT_ID}',
                    f'https://www.tango.me/@{ACCOUNT_ID}',
                    f'https://www.tango.me/profile/{ACCOUNT_ID}'):
            self.assertTrue(TangoIE._is_profile_url(url), url)


class FeedUrlTest(unittest.TestCase):
    def test_owns_the_live_index_only(self):
        for url in ('https://www.tango.me/live',
                    'https://www.tango.me/live/',
                    'https://www.tango.me/live?x=1'):
            self.assertTrue(TangoFeedIE.suitable(url), url)
        self.assertFalse(TangoFeedIE.suitable(f'https://www.tango.me/live/{STREAM}'))


if __name__ == '__main__':
    unittest.main()
