"""Offline tests for the tango HLS relay.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python -m unittest discover tests
"""

import email.message
import io
import time
import types
import unittest
from unittest import mock

from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError

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

MEDIA_PATH = '/v2/hd/live.m3u8?m=x'

LIVE_MASTER = (
    '#EXTM3U\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=2277000,RESOLUTION=1280x720\n'
    '/v2/hd/live.m3u8?m=x\n'
).encode()


class FakeRefresher:
    ie = mock.Mock()

    def __init__(self):
        self.forced = False

    def cookie_header(self):
        return 'tt=t; ttu=u; tte=1'

    def ensure(self, force=False):
        if force:
            self.forced = True


class RecordingDownloader:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def urlopen(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return playlist_response(outcome)


def relay_with(outcomes):
    refresher = FakeRefresher()
    refresher.ie = types.SimpleNamespace(
        _downloader=RecordingDownloader(outcomes), report_warning=mock.Mock())
    return TangoHlsRelay(
        refresher, 'https://cinema-in41.tango.me/v2/x/master.m3u8'), refresher


def playlist_response(body):
    headers = email.message.Message()
    headers.add_header('Content-Type', 'application/vnd.apple.mpegurl')
    return Response(io.BytesIO(body), 'https://cdn/x', headers, 200)


def make_relay():
    return TangoHlsRelay(
        FakeRefresher(),
        'https://cinema-in41.tango.me/v2/9449563a/master.m3u8?username=u&tech=lhls')


def _throttled():
    return HTTPError(Response(io.BytesIO(b''), 'https://cdn/x',
                             email.message.Message(), 403))


def http_404():
    return HTTPError(Response(io.BytesIO(b''), 'https://cdn/x',
                             email.message.Message(), 404))


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


class PlaylistWindowTest(unittest.TestCase):
    """The relay serves recent playlists as one window.

    ffmpeg drops segments when a playlist reload lands after the CDN's
    eight-segment window has moved on.  These tests pin the merge that keeps
    them available for as long as the CDN still serves them.
    """

    def setUp(self):
        self.relay = make_relay()
        self.addCleanup(self.relay.close)

    def merge(self, playlist):
        return self.relay._merge_media_playlist(MEDIA_PATH, playlist)

    def assert_segment_tags_local(self, playlist):
        """Each segment URI must carry its own tags, not a tag dump above it."""
        lines = playlist.splitlines()
        for index, line in enumerate(lines):
            if line.endswith('.ts?tech=lhls'):
                self.assertTrue(lines[index - 1].startswith('#EXTINF:'),
                                lines[index - 3:index + 1])

    def test_a_late_reload_keeps_the_segments_it_would_have_lost(self):
        self.merge(media_playlist(4, 8))
        out = self.merge(media_playlist(10, 8))
        self.assertEqual(segment_numbers(out), list(range(4, 18)))
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:4', out)
        self.assert_segment_tags_local(out)

    def test_the_window_is_capped_and_drops_the_oldest_first(self):
        self.relay.PLAYLIST_WINDOW = 10
        self.merge(media_playlist(0, 8))
        out = self.merge(media_playlist(8, 8))
        self.assertEqual(segment_numbers(out), list(range(6, 16)))
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:6', out)

    def test_a_restarted_stream_does_not_inherit_old_segments(self):
        self.merge(media_playlist(100, 8))
        out = self.merge(media_playlist(0, 8))
        self.assertEqual(segment_numbers(out), list(range(0, 8)))
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:0', out)

    def test_a_key_and_discontinuity_outlive_the_segment_that_declared_them(self):
        key = '#EXT-X-KEY:METHOD=AES-128,URI="/k/1",IV=0x0'
        self.relay.PLAYLIST_WINDOW = 3
        early = (
            '#EXTM3U\n'
            '#EXT-X-VERSION:7\n'
            '#EXT-X-TARGETDURATION:1\n'
            '#EXT-X-MEDIA-SEQUENCE:1\n'
            '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'
            f'{key}\n'
            '#EXTINF:1.000,\n'
            '/v2/sh/x/1.ts\n'
            '#EXTINF:1.000,\n'
            '/v2/sh/x/2.ts\n'
            '#EXT-X-DISCONTINUITY\n'
            '#EXTINF:1.000,\n'
            '/v2/sh/x/3.ts\n'
        )
        self.merge(early)
        out = self.merge(media_playlist(4, 3))
        self.assertEqual(segment_numbers(out), [4, 5, 6])
        self.assertIn(key, out.splitlines())
        self.assertIn('#EXT-X-DISCONTINUITY-SEQUENCE:1', out)

    def test_a_playlist_without_a_media_sequence_is_not_duplicated(self):
        playlist = media_playlist(4950, 8, sequence=False)
        self.merge(playlist)
        out = self.merge(playlist)
        self.assertEqual(segment_numbers(out), list(range(4950, 4958)))

    def test_program_date_time_stays_with_its_segment(self):
        # Real tango playlists stamp every segment; a merge that hoists the
        # stamps into the header leaves ffmpeg a playlist it cannot parse.
        playlist = (
            '#EXTM3U\n'
            '#EXT-X-VERSION:7\n'
            '#EXT-X-TARGETDURATION:1\n'
            '#EXT-X-MEDIA-SEQUENCE:7\n'
            '#EXT-X-PROGRAM-DATE-TIME:2026-11-20T13:08:45.390Z\n'
            '#EXTINF:0.999,\n'
            '/v2/sh/x/7.ts?tech=lhls\n'
            '#EXT-X-PROGRAM-DATE-TIME:2026-11-20T13:08:46.389Z\n'
            '#EXTINF:1.000,\n'
            '/v2/sh/x/8.ts?tech=lhls\n'
        )
        out = self.merge(playlist).splitlines()
        self.assertEqual(out[out.index('#EXT-X-MEDIA-SEQUENCE:7') + 1:], [
            '#EXT-X-PROGRAM-DATE-TIME:2026-11-20T13:08:45.390Z',
            '#EXTINF:0.999,',
            '/v2/sh/x/7.ts?tech=lhls',
            '#EXT-X-PROGRAM-DATE-TIME:2026-11-20T13:08:46.389Z',
            '#EXTINF:1.000,',
            '/v2/sh/x/8.ts?tech=lhls',
        ])
        self.assertEqual(out.count('#EXT-X-MEDIA-SEQUENCE:7'), 1)

    def test_the_relay_serves_the_merged_playlist_to_ffmpeg(self):
        relay, _ = relay_with([
            media_playlist(4950, 4).encode(),
            media_playlist(4956, 4).encode(),
        ])
        self.addCleanup(relay.close)
        relay.fetch(MEDIA_PATH)
        status, _, body = relay.fetch(MEDIA_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(segment_numbers(body.decode()), [4950, 4951, 4952,
                                                         4953, 4956, 4957, 4958, 4959])


def media_playlist(start, count, *, sequence=True):
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-TARGETDURATION:1']
    if sequence:
        lines.append(f'#EXT-X-MEDIA-SEQUENCE:{start}')
    for number in range(start, start + count):
        lines.append(f'#EXT-X-PROGRAM-DATE-TIME:2026-11-20T13:08:{number % 60:02d}.000Z')
        lines.append('#EXTINF:1.000,')
        lines.append(f'/v2/sh/x/{number}.ts?tech=lhls')
    return '\n'.join(lines) + '\n'


def segment_numbers(playlist):
    return [int(line.rsplit('/', 1)[-1].split('.')[0])
            for line in playlist.splitlines()
            if line and not line.startswith('#')]


class UpstreamRequestTest(unittest.TestCase):
    def test_the_upstream_request_carries_a_timeout_and_the_token(self):
        relay, _ = relay_with([MASTER])
        self.addCleanup(relay.close)
        relay.fetch(relay.master_path)
        request = relay.refresher.ie._downloader.requests[0]
        self.assertEqual(request.extensions['timeout'], 30)
        self.assertEqual(request.headers['Cookie'], 'tt=t; ttu=u; tte=1')
        self.assertIn('https://www.tango.me/', request.headers['Referer'])

    def test_a_transport_error_forces_a_refresh(self):
        relay, refresher = relay_with([TransportError('reset'), MASTER])
        self.addCleanup(relay.close)
        status, _, _ = relay.fetch(relay.master_path)
        self.assertEqual(status, 200)
        self.assertTrue(refresher.forced)


class ThrottlingFallbackTest(unittest.TestCase):
    def test_replays_last_playlist_when_the_token_is_throttled(self):
        relay, refresher = relay_with([_throttled(), _throttled()])
        self.addCleanup(relay.close)
        path = relay.master_path
        cached = b'#EXTM3U\n#EXTINF:1.0,\nhttps://cdn/1.ts\n'
        relay._cache[path] = (cached, 'application/vnd.apple.mpegurl', time.time())
        with mock.patch('time.sleep'):
            status, _, body = relay.fetch(path)
        self.assertEqual(status, 200)
        self.assertEqual(body, cached)
        # Both throttled replies were consumed, so the mid-window replay
        # branch served the cache (not the early-ENDLIST short-circuit).
        self.assertEqual(len(refresher.ie._downloader.requests), 2)
        refresher.ie.report_warning.assert_called_once()
        self.assertIn('throttled', refresher.ie.report_warning.call_args[0][0])

    def test_an_ended_cached_playlist_is_served_without_an_upstream_request(self):
        relay, refresher = relay_with([MASTER])
        self.addCleanup(relay.close)
        path = relay.master_path
        ended = b'#EXTM3U\n#EXT-X-ENDLIST\n'
        relay._cache[path] = (ended, 'application/vnd.apple.mpegurl', time.time())
        status, _, body = relay.fetch(path)
        self.assertEqual(status, 200)
        self.assertEqual(body, ended)
        self.assertEqual(len(refresher.ie._downloader.requests), 0)

    def test_raises_when_throttled_without_a_cached_playlist(self):
        relay, _ = relay_with([_throttled(), _throttled()])
        self.addCleanup(relay.close)
        with mock.patch('time.sleep'):
            with self.assertRaises(HTTPError):
                relay.fetch('/v2/nothing/live.m3u8')

    def test_ends_the_recording_when_the_token_stays_gone(self):
        relay, _ = relay_with([_throttled(), _throttled()])
        self.addCleanup(relay.close)
        path = relay.master_path
        relay._cache[path] = (
            b'#EXTM3U\n#EXTINF:1.0,\nhttps://cdn/1.ts\n',
            'application/vnd.apple.mpegurl', time.time() - 3600)
        with mock.patch('time.sleep'):
            status, _, body = relay.fetch(path)
        self.assertEqual(status, 200)
        self.assertTrue(body.endswith(b'#EXT-X-ENDLIST\n'))

    def test_does_not_end_a_master_playlist(self):
        self.assertEqual(
            _end_playlist(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n/a.m3u8\n'),
            b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n/a.m3u8\n')


class EndedStreamTest(unittest.TestCase):
    def test_a_served_media_playlist_that_404s_ends_cleanly(self):
        relay, refresher = relay_with([LIVE_MASTER, MEDIA, http_404()])
        self.addCleanup(relay.close)
        relay.fetch(relay.master_path)     # emits MEDIA_PATH
        relay.fetch(MEDIA_PATH)            # serves and caches it
        status, _, body = relay.fetch(MEDIA_PATH)
        self.assertEqual(status, 200)
        self.assertTrue(body.endswith(b'#EXT-X-ENDLIST\n'))
        refresher.ie.report_warning.assert_called_once()

    def test_a_path_the_relay_never_served_still_raises(self):
        relay, _ = relay_with([http_404()])
        self.addCleanup(relay.close)
        with self.assertRaises(HTTPError):
            relay.fetch('/v2/unknown/x.m3u8')

    def test_the_ended_playlist_is_reused(self):
        relay, refresher = relay_with([LIVE_MASTER, MEDIA, http_404()])
        self.addCleanup(relay.close)
        relay.fetch(relay.master_path)
        relay.fetch(MEDIA_PATH)
        relay.fetch(MEDIA_PATH)
        relay.fetch(MEDIA_PATH)
        self.assertEqual(len(refresher.ie._downloader.requests), 3)


if __name__ == '__main__':
    unittest.main()
