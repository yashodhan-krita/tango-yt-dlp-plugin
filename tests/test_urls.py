"""Offline tests for tango.me URL routing.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable,
and with an empty config dir so an installed copy of the plugin is not loaded::

    XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
        /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python -m unittest discover -s tests
"""

import unittest

from yt_dlp_plugins.extractor.tango import TangoCdnIE, TangoFeedIE, TangoIE

from tests.fake_net import ACCOUNT_ID, ALIAS, STREAM_ID as STREAM


class StreamUrlTest(unittest.TestCase):
    def test_accepts_every_stream_and_alias_form(self):
        for url in (
            f'https://www.tango.me/stream/{STREAM}',
            f'https://www.tango.me/live/{STREAM}',
            f'https://www.tango.me/{ALIAS}',
            f'https://www.tango.me/{ALIAS}/',
            f'https://www.tango.me/profile/{ALIAS}',
            f'https://www.tango.me/{ALIAS}?tab=about',
            f'https://m.tango.me/{ALIAS}',
            f'https://www.tango.me/@{ALIAS}',
        ):
            match = TangoIE._match_valid_url(url)
            self.assertTrue(match, url)
            self.assertEqual(match.group('id'), STREAM if STREAM in url
                             else f'@{ALIAS}' if '@' in url else ALIAS, url)

    def test_reserved_routes_are_not_aliases(self):
        for url in ('https://www.tango.me/stream/',
                    'https://www.tango.me/profile/',
                    'https://www.tango.me/search',
                    'https://www.tango.me/live',
                    'https://www.tango.me/settings/privacy',
                    'https://www.tango.me/store/x',
                    'https://www.tango.me/live/profile'):
            self.assertFalse(TangoIE.suitable(url), url)

    def test_route_names_ignore_case(self):
        for url in ('https://www.tango.me/Live', 'https://www.tango.me/LIVE/'):
            self.assertFalse(TangoIE.suitable(url), url)
            self.assertTrue(TangoFeedIE.suitable(url), url)
        match = TangoIE._match_valid_url(f'https://www.tango.me/Stream/{STREAM}')
        self.assertEqual(match.group('kind').lower(), 'stream')
        self.assertEqual(match.group('id'), STREAM)
        # Aliases keep their case.
        self.assertEqual(TangoIE._match_valid_url(
            'https://www.tango.me/MixedCase_Alias').group('id'), 'MixedCase_Alias')

    def test_profile_and_stream_forms_are_told_apart(self):
        def kind(url):
            match = TangoIE._match_valid_url(url)
            return match.group('kind') if match else None

        account = f'https://www.tango.me/profile/{ACCOUNT_ID}'
        self.assertTrue(TangoIE.suitable(account))
        self.assertEqual(TangoIE._match_valid_url(account).group('id'), ACCOUNT_ID)
        self.assertEqual(kind(account), 'profile')
        for url in (f'https://www.tango.me/stream/{ACCOUNT_ID}',
                    f'https://www.tango.me/live/{ACCOUNT_ID}',
                    f'https://m.tango.me/stream/{ACCOUNT_ID}'):
            self.assertIn(kind(url), ('stream', 'live'), url)
        for url in (f'https://www.tango.me/{ACCOUNT_ID}',
                    f'https://www.tango.me/@{ACCOUNT_ID}',
                    f'https://www.tango.me/profile/{ACCOUNT_ID}'):
            self.assertNotIn(kind(url), ('stream', 'live'), url)


class CdnUrlTest(unittest.TestCase):
    def test_owns_cdn_playlists_only(self):
        base = f'https://cinema-in41.tango.me/v2/{"1" * 32}/'
        for name in ('master', 'live', 'hd', 'preview'):
            url = f'{base}{name}.m3u8'
            self.assertTrue(TangoCdnIE.suitable(url), url)
            # The identity extractor must not read a CDN host as a profile.
            self.assertFalse(TangoIE.suitable(url), url)
        self.assertTrue(TangoCdnIE.suitable(
            f'{base}live.m3u8?token=x&username=y'))
        for url in (f'{base}5123.ts',
                    base,
                    f'https://www.tango.me/v2/{"1" * 32}/master.m3u8',
                    f'https://www.tango.me/stream/{STREAM}'):
            self.assertFalse(TangoCdnIE.suitable(url), url)

    def test_a_cdn_path_on_the_site_host_matches_nothing(self):
        # Measured 2026-11-21: www.tango.me/v2/<hash>/master.m3u8 is the SPA
        # (200 text/html), neither the CDN nor an alias named "v2".
        for host in ('www.tango.me', 'tango.me', 'm.tango.me'):
            url = f'https://{host}/v2/{"1" * 32}/master.m3u8'
            self.assertFalse(TangoIE.suitable(url), url)
            self.assertFalse(TangoCdnIE.suitable(url), url)


class FeedUrlTest(unittest.TestCase):
    def test_owns_the_live_index_only(self):
        for url in ('https://www.tango.me/live',
                    'https://www.tango.me/live/',
                    'https://www.tango.me/live?x=1'):
            self.assertTrue(TangoFeedIE.suitable(url), url)
        self.assertFalse(TangoFeedIE.suitable(f'https://www.tango.me/live/{STREAM}'))


if __name__ == '__main__':
    unittest.main()
