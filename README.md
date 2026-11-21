# tango-yt-dlp-plugin

A yt-dlp extractor for live streams on [tango.me](https://www.tango.me). It
records with ffmpeg, works anonymously, and keeps running past the site's
ten-second CDN token by refreshing it through a small local HLS relay.

## Requirements

- yt-dlp. Tested with 2026.10.26 (the Homebrew build, CPython 3.15). Older
  versions have not been tested; the code needs `Request(extensions=...)`,
  per-request `proxies=`, `UserNotLive` and `_yield_json_ld`.
- ffmpeg on `PATH` (yt-dlp hands live HLS to ffmpeg).

## Install

There are three supported installs. They differ in where yt-dlp finds the
plugin.

| Install | Command | When to use it |
| --- | --- | --- |
| Symlink into the config dir | `./install.sh` | A checkout you edit. yt-dlp loads it from `~/.config/yt-dlp/plugins/tango-yt-dlp/`; edits take effect on the next run. |
| `pipx inject` | `pipx inject yt-dlp /path/to/tango-yt-dlp-plugin` | yt-dlp installed with pipx. Installs the plugin into yt-dlp's own pipx venv. |
| pip into yt-dlp's environment | `/path/to/that/python -m pip install /path/to/tango-yt-dlp-plugin` | yt-dlp installed with pip in a venv. Use that venv's interpreter. |

A pip install is only visible to the Python environment that runs yt-dlp. A
`pip install` into some other environment (the system Python, a different
venv, or next to a Homebrew yt-dlp) succeeds and does nothing: yt-dlp never
sees the plugin. For a pip, pipx or Homebrew yt-dlp,
`head -1 "$(command -v yt-dlp)"` names the interpreter it runs on; install
into that one, or use the symlink. The symlink and a venv pip install were
checked with yt-dlp 2026.10.26 (2026-11-20); `pipx inject` has not been run
`[needs live check: pipx inject, then yt-dlp -v shows Tango under Extractor Plugins]`.

`install.sh` creates the symlink, refuses to replace a real directory at the
link path (`ln -sfn` would nest a link inside it), and then checks that
yt-dlp loaded the plugin. That check runs `--flat-playlist` on
`https://www.tango.me/live`, which makes two requests to tango.me: a visitor
check-in and the live catalog. It joins no stream.

Without installing, point yt-dlp at the **parent** folder of this repo:

```sh
yt-dlp --plugin-dirs ~/Projects -S res "https://www.tango.me/<alias>"
```

`--plugin-dirs` takes a container folder, not this project folder: yt-dlp
scans its children for `yt_dlp_plugins/`.

## Use

Every form resolves to the same stream:

```sh
yt-dlp -S res "https://www.tango.me/stream/<stream-id>"
yt-dlp -S res "https://www.tango.me/live/<stream-id>"
yt-dlp -S res "https://www.tango.me/<alias>"
yt-dlp -S res "https://www.tango.me/profile/<alias>"
yt-dlp -S res "https://www.tango.me/@<alias>"
yt-dlp -S res "https://www.tango.me/profile/<account-id>"
```

`-S res` picks the highest resolution. Do not use `-f 1`: format ids follow
the order of the master playlist, and that order is not quality order. One
master sampled on 2026-11-21 listed 1280x720 first and 640x360 second; an
earlier one (2026-11-20) listed the low variant first, and `-f 1` recorded
360x640. `-f best` also works.

`tango.me/<alias>` and the shared `tango.me/@<alias>` form are looked up
through the profile API, so they work for aliases you have never opened in a
browser (`test_alias_and_at_alias_reach_the_same_stream`).

List who is live right now:

```sh
yt-dlp --flat-playlist "https://www.tango.me/live"
```

Wait for an offline profile to go live:

```sh
yt-dlp --wait-for-video 60 -S res "https://www.tango.me/@<alias>"
```

Every retry resolves the profile again, so the stream id that appears later is
found even though the profile had none at the start. To track someone across
separate broadcasts, loop it with an archive so a broadcast is not
downloaded twice. The archive entry is the stream id, so it is per
broadcast, not per broadcaster:

```sh
while :; do
    yt-dlp --wait-for-video 60 --download-archive ~/.cache/tango-archive.txt \
        -S res "https://www.tango.me/@<alias>" || true
    sleep 30
done
```

Stop the loop with Ctrl-C while it is in `sleep 30`, not while yt-dlp is
recording. yt-dlp treats Ctrl-C on a live download as success, so the
stream id goes into the archive, and if the loop keeps running it skips the
rest of that broadcast.

Profile URLs accept an alias, `@alias`, `/profile/<alias>`, or the site's
22-char account id (`/profile/<account-id>`). The extractor reads the public
profile page for the display name, avatar, bio and follower count instead of
guessing them from the stream title; an account-id URL is the same page under
its canonical address. Because the URL form decides the meaning, a bare
`/<22-char-id>` is a profile, not a stream; use `/stream/<id>` when you mean
the stream id itself (`test_a_profile_account_id_is_never_read_as_a_stream_id`).

A profile URL always means *the current live stream*. If the broadcast ends
between resolution and lookup, it reports `not live` so `--wait-for-video`
keeps waiting (`test_a_terminated_profile_waits_instead_of_downloading`). A
`/stream/<id>` URL is the archival form: if `watch` still returns the
stream's `completeListUrl`, that finished recording is downloaded whole (the
`ENDED` case of `test_only_a_timed_live_stream_carries_a_start_time` takes
the fallback, and
`test_an_ended_playlist_is_served_whole_and_a_live_one_is_windowed` serves
it whole); once
tango removes the stream, it reports `not live` too.

Stop a recording with Ctrl-C. yt-dlp finalises the file and MP4-fixes it.

A live recording is dated. The watch document's `duration` is the elapsed
broadcast time, so the extractor sets `timestamp` to the broadcast start and
yt-dlp turns it into `upload_date`; `%(upload_date)s` in an output template is
the day the stream began. Two `--print timestamp` runs 60 s apart on
2026-11-21 gave values one second apart. `duration` itself is `NA` for a
live stream.

Every Tango result carries `age_limit` 18. That is a policy choice, not read
from the site (six catalog items sampled on 2026-11-21 all had `isNsfw`
false), so `--age-limit` below 18 skips every Tango stream.

### Coming from yt-dlp issue #11433?

[yt-dlp#11433](https://github.com/yt-dlp/yt-dlp/issues/11433) reports
`Unsupported URL` for `https://tango.me/stream/noscript.html`. That is the page
tango shows a client without JavaScript, and it carries no stream id. Pass the
original `https://www.tango.me/stream/<id>` URL or a profile URL instead; this
plugin names the no-script page in its error rather than guessing
(`test_the_noscript_page_is_named_without_a_request`).

The workaround in that thread, a copied `master.m3u8?token=...` link, stops
after 2-3 minutes: the CDN answers 401 to `?token=` on its own and wants a
cookie triplet that expires every ten seconds. Given a stream or profile URL,
this plugin joins the stream through tango's API, reads the real playlist URL
from it, and refreshes that cookie for as long as the recording runs.

The copied CDN playlist link is accepted as well, under any name (any
`*.m3u8` under `/v2/<hash>/`, such as `master.m3u8` or `live.m3u8`):

```sh
yt-dlp -S res 'https://cinema-in41.tango.me/v2/<hash>/live.m3u8?token=...&username=...'
```

The token in its query is ignored, because it dies within seconds. The plugin
checks in as a visitor, takes a fresh token and relays the playlist, so the
recording runs past the token window. Measured on 2026-11-21: a fresh triplet
authorises the master and a rendition (both 200) with no `watch` call and no
visit to the stream page (`test_a_copied_cdn_url_is_authorised_by_the_triplet_alone`).
The copied link does not name the broadcaster, so the title carries the hash
and no profile metadata is read.

Do not paste the output of `yt-dlp -v` for a copied link: it prints the URL
you gave it, including its `token=` and `username=` values.

### Anonymous vs logged in

| | Anonymous | `--cookies-from-browser chrome` |
| --- | --- | --- |
| Public streams | yes | yes |
| Followers-only / private | no | intended; needs an account that follows the broadcaster `[needs live check]` |
| Formats | 640x360, 1280x720 | same |
| Extra setup | none | a logged-in Chrome profile |
| Check-in per run | one visitor check-in | one visitor check-in on top of your account cookies |

No account is needed for normal use. Anonymous viewers get the same renditions
as logged-in ones; the difference is only which streams are visible.

### Formats

```
ID  EXT  RESOLUTION
0   mp4  640x360
1   mp4  1280x720
```

The order above is an example; it varies between masters (see `-S res`
under Use). For a live stream the 0/1 ids follow master order; a finished
recording read from `completeListUrl` gets ids based on bandwidth.

- The manifest claims `RESOLUTION=1280x720`, but broadcasts are often
  portrait. The finished file can be `720x1280`; that is what the site sends.
- The manifest has no `CODECS` attribute, so `-F` shows `unknown` for
  vcodec/acodec. The actual stream is H.264 + AAC.

### Logged-in and private streams

Anonymous access covers public streams. For followers-only or private streams,
reuse your browser session:

```sh
yt-dlp -S res --cookies-from-browser chrome "https://www.tango.me/<alias>"
```

The extractor still performs the site's visitor check-in, because the CDN token
endpoint only issues tokens to a visitor identity. Measured on 2026-11-21, the
check-in reply expires the account cookies (`Tango-WST`, `Tango-SD`,
`Tango-RT`), and the account's `me` call goes from 200 to 401. The extractor
snapshots those cookies and puts back any the reply removed, so `watch` sees
them again (`test_the_account_session_survives_the_check_in`). The token
request itself leaves the jar alone. The check-in does not: it runs once
before the relay starts, and again on the relay thread if a token request
answers 403 mid-recording (`_issue_token` forces a fresh visitor). For the
length of that one request the account cookies are expired in the jar, and
`_check_in` puts them back in a `finally`
(`test_the_jar_never_lacks_the_account_during_an_extraction` checks the jar
before every request, including those after the forced check-in). A
`--cookies` file saved by a Ctrl-C inside that window could miss them; the
mid-relay case has not been observed live `[needs live check]`.
Whether carrying them on the token calls changes the 403 recovery is
`[needs live check]`. Whether the
site accepts the restored cookies as a live session is `[needs live check]`:
it needs a signed-in browser, a run with `--cookies after.txt`, and a 200 from
`me` with that file. No followers-only broadcast has been recorded yet.

## How it works

The web player talks to `https://www.tango.me/api` (not the old
`gateway.tango.me`):

| Step | Request | Why |
| --- | --- | --- |
| 1 | `POST /visitor-lobby/api/public/visitors/v1/check-in?appId=tango` | sets `Tango-VT` (visitor JWT), `Tango-SD`, `Tango-DeviceId` |
| 2 | `POST /visitors/stream/client/v2/stream/{id}/watch?requestId=` | registers the viewer and returns the stream document (status, HLS URLs, anchor) |
| 3 | `GET /stream/token-data/v1/web/visitor` | returns `{token, username, expireAt}` with a 10-second TTL |

`watch` supplies stream metadata and the playlist URL; it is not an
authorisation step for the CDN (measured 2026-11-21, see the KB).

The CDN (`cinema-*.tango.me`) guards **playlists** with the cookie triplet
`tt` / `ttu` / `tte`; media segments are public. After ten seconds every
playlist request returns 401, which is why a naive download dies a few seconds
in.

yt-dlp always routes live HLS to ffmpeg, and ffmpeg cannot refresh cookies.
So the extractor starts `TangoHlsRelay` on `127.0.0.1` and gives ffmpeg that
URL. On every playlist reload the relay asks for a fresh token, fetches the
playlist, and rewrites segment URIs to absolute CDN URLs so the video itself
does not pass through Python. Master playlists keep pointing at the relay,
because variant playlists need the token too.

The CDN's playlist holds only the newest eight seconds, and ffmpeg asks for
the segment after the last one it played. If ffmpeg falls behind the live
edge, it would therefore skip segments and leave a short file, so the relay
keeps the union of recent playlists and serves about 45 seconds of them. The
relay does not poll; it merges only when ffmpeg asks for a playlist, so a
gap of more than the CDN's ~8 s window between two reloads still loses the
segments in between. The window sits
inside the CDN's roughly 70-second segment retention (measured 2026-11-20).

A proxy would swallow that loopback URL: yt-dlp passes `--proxy` (and
`http_proxy` from the environment) on to ffmpeg, which applies it to every
request it makes. The extractor appends `127.0.0.1` and `localhost` to
`no_proxy`/`NO_PROXY` when it runs, so the relay is reached directly while
the API and CDN traffic keeps going through the proxy
(`test_extraction_exempts_loopback_from_the_proxy`).

A recording is downloaded by ffmpeg through the loopback relay, finished
recordings included. yt-dlp's native HLS downloader ignores `no_proxy` and
would send the loopback URL to the proxy, so the relayed formats are marked
for ffmpeg (`test_relayed_formats_are_downloaded_by_ffmpeg`).
`--downloader native` can still select it for a finished recording; that is
your choice, not the default, and it fails behind `--proxy`.

ffmpeg only honours an `http://` proxy. Measured: with `--proxy http://…`
ffmpeg's CDN segment fetches go through the proxy; with a `socks5://` or
`https://` proxy ffmpeg ignores it and fetches the CDN segments directly.
The plugin has no fix for that, and `--downloader native` is not an
alternative for a recording, for the reason above.

If the token endpoint refuses, the relay replays the last playlist it
fetched (up to two minutes old) instead of failing. ffmpeg sees an unchanged
playlist and stalls until the token returns; segments are lost only if that
takes longer than the CDN keeps them. A refusal is also retried with a fresh
visitor identity (`test_a_throttled_token_recovers_with_a_fresh_visitor`).
After a failed refresh the relay waits 5 s before the next attempt (2 s for a
forced one), but only while the cached token is still unexpired
(`test_a_failed_refresh_cools_down_while_the_token_still_works`). Once it has
expired there is no cool-down, so a long throttle pays a full refresh cycle on
every playlist reload, about six token calls and two check-ins
(`test_an_expired_token_gets_no_cool_down`). If
the token stays unavailable beyond that window, the relay appends
`#EXT-X-ENDLIST` so ffmpeg finalises a playable file rather than the
recording failing (`test_ends_the_recording_when_the_token_stays_gone`).

## Limits

- **No DVR.** Live playlists hold an eight-second sliding window, so
  `--live-from-start` cannot work. Recording starts at the live edge. The
  relay serves a merged window of about 45 seconds so ffmpeg can run that far
  behind the live edge without dropping segments; that is not seekable
  history, only slack.
- **`--proxy` must be `http://` for the video.** ffmpeg ignores a
  `socks5://` or `https://` proxy and fetches the CDN segments directly
  (measured). No fix; `--downloader native` does not help for a recording.
- **Token refresh is cheap.** One token per ~8 seconds, about 450 per hour.
  Measured on 2026-11-20 on a single visitor identity: 400 token calls in
  188 s and 200 check-in-plus-token rounds in 191 s, none refused.
- **Viewer count.** Calling `watch` adds one viewer, so the reported
  `concurrent_view_count` includes this download. `-F`, `--simulate` and
  `--print` call `watch` too.
- **Ended streams.** Tango drops a stream from its routes shortly after the
  end: the profile stream lookup 404s and `watch` answers
  `FAILED_TERMINATED`, with no HLS URL left anywhere. A fresh download
  normally has to start while the stream is live; the `completeListUrl`
  fallback only covers the short window after the end in which `watch` still
  returns the recording.
  If a stream ends *during* a recording, the CDN starts answering 404 or 410
  for playlists the relay had already served; the relay marks the last one
  with `#EXT-X-ENDLIST`, so ffmpeg finalises a playable file instead of the
  download failing (`test_a_served_media_playlist_that_404s_ends_cleanly`).
  One 404 is enough to end the recording. A 10-minute poll on 2026-11-21 saw
  507 one-second playlist fetches, all 200, so no transient 404 has been seen.

## Troubleshooting

- `Unsupported URL`: the plugin is not loaded. Check
  `yt-dlp --verbose … | grep 'Extractor Plugins'`, and see the pip warning
  under Install.
- `ffmpeg not found`: install ffmpeg; live HLS cannot go through yt-dlp's
  native downloader.
- `Could not refresh stream token … 403`: the token endpoint refused. The
  extractor retries with a fresh visitor identity and keeps replaying the last
  playlist for two minutes before ending the file cleanly.
- `The stream has ended`: tango removed the stream from every route; only
  live streams carry an HLS URL.
- Recording stalls behind a proxy: the plugin already exempts the loopback
  relay from it, so a stall means the proxy is failing the CDN traffic
  itself.

When you report a problem, a verbose log is useful, but for a copied CDN link
remove the `token=` and `username=` values first.

## Tests

The relay logic (playlist rewriting, throttle replay) and the visitor-session
rules are covered offline, and the extractor itself runs end to end against a
fake transport that enforces the measured request contract, so extraction,
relay wiring and failure paths are tested without the network. 102 tests.

Run the suite with the interpreter that ships with yt-dlp, and with an empty
config dir:

```sh
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. \
  /opt/homebrew/Cellar/yt-dlp/2026.10.26/libexec/bin/python -m unittest discover -s tests
```

`XDG_CONFIG_HOME` matters on a machine with the plugin installed. Importing
`yt_dlp` installs its plugin finder, which serves `yt_dlp_plugins` from
`~/.config/yt-dlp/plugins/*` ahead of `PYTHONPATH`, so without it the suite
tests the installed copy instead of this checkout.
`test_the_module_under_test_is_this_checkout` fails when that happens. The
Homebrew path is version-pinned; after an upgrade use
`$(brew --prefix yt-dlp)/libexec/bin/python`.

## Files

- `yt_dlp_plugins/extractor/tango.py`: the extractors (`tango`,
  `tango:live`, `tango:cdn`) and the relay
- `tests/test_environment.py`: fails if the suite imported another copy
- `tests/test_relay.py`: offline relay tests
- `tests/test_extract.py`: end-to-end extraction through the real extractor
  with a fake transport
- `tests/fake_net.py`: the fake transport those tests run on
- `tests/fixtures/`: scrubbed fixtures, provenance in its README
- `tests/test_session.py`: visitor token, identity reset and refresh tests
- `tests/test_urls.py`: URL routing tests
- `install.sh`: plugin-dir installer
- `pyproject.toml`: wheel metadata (the wheel holds only `yt_dlp_plugins/`)
- `PLAN.md`: the original design record (2026-11-20); historical
- `docs/kb/tango-me.md`: technical knowledge base: contract, failure modes,
  measurements, traps
- `docs/handoff-2026-11-21-tango-plugin.md`: project handoff: state, open
  work, commands and known traps
- `docs/superpowers/`: design, plans and review records
- `contrib/tango_chat_recorder.py`: unused chat-to-SRT sketch for the retired
  gateway API; not part of the plugin and known not to run as is

## License

[Unlicense](LICENSE): public domain, the same licence as yt-dlp itself.
Use it, change it, ship it, no attribution required.
