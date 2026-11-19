"""Offline tests for the tango HLS relay.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python -m unittest discover tests
"""

import io
import time
import urllib.error
import unittest
from unittest import mock

from yt_dlp_plugins.extractor.tango import TangoHlsRelay, _end_playlist

MASTER = (
    '#EXTM3U\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=2277000,RESOLUTION=1280x720\n'
    '/v2/a235d4123e1c7c969de1afd213008979/live.m3u8?m=9449563a&tech=lhls\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=928000,RESOLUTION=640x360\n'
    'https://cinema-in41.tango.me/v2/acaebe92c7f50d3222021d85c160b41b/live.m3u8?m=9449563a\n'
).encode()

MEDIA = (
    '#EXTM3U\n'
    '#EXT-X-VERSION:7\n'
    '#EXT-X-TARGETDURATION:1\n'
    '#EXTINF:1.000,\n'
    '/v2/sh/a235d4123e1c7c969de1afd213008979/7c4721cacf1ae0c2f50dade662bdcbe4/4950.ts?tech=lhls\n'
    '#EXTINF:1.000,\n'
    'https://cinema-in41.tango.me/v2/sh/a235d4123e1c7c969de1afd213008979/69c931ee/4951.ts?tech=lhls\n'
).encode()


class FakeRefresher:
    ie = mock.Mock()

    def cookie_header(self):
        return 'tt=t; ttu=u; tte=1'

    def ensure(self, force=False):
        pass


def make_relay():
    return TangoHlsRelay(
        FakeRefresher(),
        'https://cinema-in41.tango.me/v2/9449563a/master.m3u8?username=u&tech=lhls')


def _throttled():
    return urllib.error.HTTPError(
        'https://cdn', 403, 'Forbidden', None, io.BytesIO(b''))


class PlaylistRewriteTest(unittest.TestCase):
    def setUp(self):
        self.relay = make_relay()

    def tearDown(self):
        self.relay.close()

    def test_master_playlists_stay_on_the_relay(self):
        out = self.relay._rewrite_playlist(
            MASTER, 'https://cinema-in41.tango.me/v2/9449563a/master.m3u8').decode()
        uris = [line for line in out.splitlines() if line and not line.startswith('#')]
        self.assertEqual(uris, [
            '/v2/a235d4123e1c7c969de1afd213008979/live.m3u8?m=9449563a&tech=lhls',
            '/v2/acaebe92c7f50d3222021d85c160b41b/live.m3u8?m=9449563a',
        ])

    def test_media_playlists_point_segments_at_the_cdn(self):
        out = self.relay._rewrite_playlist(
            MEDIA, 'https://cinema-in41.tango.me/v2/hd/live.m3u8?m=x').decode()
        uris = [line for line in out.splitlines() if line and not line.startswith('#')]
        self.assertEqual(uris, [
            'https://cinema-in41.tango.me/v2/sh/a235d4123e1c7c969de1afd213008979/7c4721cacf1ae0c2f50dade662bdcbe4/4950.ts?tech=lhls',
            'https://cinema-in41.tango.me/v2/sh/a235d4123e1c7c969de1afd213008979/69c931ee/4951.ts?tech=lhls',
        ])


class ThrottlingFallbackTest(unittest.TestCase):
    def setUp(self):
        self.relay = make_relay()

    def tearDown(self):
        self.relay.close()

    def test_replays_last_playlist_when_the_token_is_throttled(self):
        path = self.relay.master_path
        self.relay._cache[path] = (b'#EXTM3U\n#EXT-X-ENDLIST\n',
                                   'application/vnd.apple.mpegurl', time.time())
        with mock.patch('urllib.request.urlopen', side_effect=_throttled()), \
                mock.patch('time.sleep'):
            status, _, body = self.relay.fetch(path)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'#EXTM3U\n#EXT-X-ENDLIST\n')

    def test_raises_when_throttled_without_a_cached_playlist(self):
        with mock.patch('urllib.request.urlopen', side_effect=_throttled()), \
                mock.patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                self.relay.fetch('/v2/nothing/live.m3u8')

    def test_ends_the_recording_when_the_token_stays_gone(self):
        path = self.relay.master_path
        self.relay._cache[path] = (
            b'#EXTM3U\n#EXTINF:1.0,\nhttps://cdn/1.ts\n',
            'application/vnd.apple.mpegurl', time.time() - 3600)
        with mock.patch('urllib.request.urlopen', side_effect=_throttled()), \
                mock.patch('time.sleep'):
            status, _, body = self.relay.fetch(path)
        self.assertEqual(status, 200)
        self.assertTrue(body.endswith(b'#EXT-X-ENDLIST\n'))

    def test_does_not_end_a_master_playlist(self):
        self.assertEqual(
            _end_playlist(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n/a.m3u8\n'),
            b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n/a.m3u8\n')


if __name__ == '__main__':
    unittest.main()
