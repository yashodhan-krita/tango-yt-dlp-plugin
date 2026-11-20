# Captured tango.me fixtures

Scrubbed tango.me fixture data.  Only `watch.json` is a live capture (the
watch response from `/tmp/watch.bin`, scrubbed); every other file is
reconstructed from the API contract decoded in `PLAN.md`, originally from
check-in-cookie-jar captures (`curl -c jar.txt -b jar.txt`).  Ids, names,
thumbnails and CDN hashes use the fixed values in `tests/fake_net.py`
(`STREAM_ID`, account id `GGGGHHHHIIIIJJJJKKKKLL`, CDN
`cinema-in41.tango.me`, hashes `'1' * 32` / `'2' * 32` / `'3' * 32`).  No
tokens, account ids or personal data are stored here; `check_in` replies its
cookies through the route table in `base_routes()` instead of the fixture
file.

| File | Method and URL | Request body |
|---|---|---|
| `check_in.json` | `POST https://www.tango.me/api/visitor-lobby/api/public/visitors/v1/check-in?appId=tango` | `{}` (`Content-Type: application/json`) |
| `token.json` | `GET https://www.tango.me/api/stream/token-data/v1/web/visitor` | — |
| `watch.json` | `POST https://www.tango.me/api/visitors/stream/client/v2/stream/AAAABBBBCCCCDDDDEEEEFF/watch?requestId=` | empty (`Content-Type: application/json`) |
| `watch_wrong_id.json` | `POST https://www.tango.me/api/visitors/stream/client/v2/stream/<bad-id>/watch?requestId=` | empty (`Content-Type: application/json`) |
| `watch_terminated.json` | `POST https://www.tango.me/api/visitors/stream/client/v2/stream/<ended-id>/watch?requestId=` | empty (`Content-Type: application/json`) |
| `watch_bad_request.json` | `POST https://www.tango.me/api/visitors/stream/client/v2/stream/<audio-room-id>/watch?requestId=` | empty (`Content-Type: application/json`) |
| `alias_convert.txt` | `GET https://www.tango.me/api/visitors/profilealias/api/alias/convert?alias=tango_tester` | — |
| `stream_id.txt` | `GET https://www.tango.me/api/showroom-streams/api/public/stream/v1/id?broadcasterId=GGGGHHHHIIIIJJJJKKKKLL` | — |
| `profile.html` | `GET https://www.tango.me/stream/AAAABBBBCCCCDDDDEEEEFF` | — |
| `profile_page.html` | `GET https://www.tango.me/tango_tester` | — |
| `profile_page_account.html` | `GET https://www.tango.me/GGGGHHHHIIIIJJJJKKKKLL` | — |
| `feed.json` | `POST https://www.tango.me/api/showroom-streams/api/public/web/catalog/v1?traceId=<uuid>` | `{"size":30,"platform":"web_desktop","hMarketingCampaign":false}` |
| `master.m3u8` | `GET https://cinema-in41.tango.me/v2/11111111111111111111111111111111/master.m3u8` | — |
| `hd.m3u8` | `GET https://cinema-in41.tango.me/v2/22222222222222222222222222222222/hd.m3u8` | — |
| `ld.m3u8` | `GET https://cinema-in41.tango.me/v2/33333333333333333333333333333333/ld.m3u8` | — |

`watch.json` mirrors the real watch response captured in `/tmp/watch.bin`
(stream id, account ids, thumbnail and the four CDN hashes scrubbed; viewer
and point counters kept as observed).  It is the only row captured fresh.
`watch_terminated.json` and `watch_bad_request.json` are the observed
`{"code": ...}` shapes recorded in `PLAN.md` rather than fresh captures: a
terminated stream and an audio room both answer the same watch call, but the
capture session predates this fixture set, so their bodies are reconstructed
from the documented failure modes.  The remaining rows (`check_in.json`,
`token.json`, `watch_wrong_id.json`, `alias_convert.txt`, `stream_id.txt`,
`profile.html`, `profile_page.html`, `profile_page_account.html`,
`feed.json`, `master.m3u8`, `hd.m3u8`, `ld.m3u8`) are likewise reconstructed
from the shapes recorded in `PLAN.md` (and, for the two profile pages, the
pages the extractor parses) rather than captured fresh.  `profile.html` is a
stream page (the name predates profile tracking); the `profile_page` fixtures
mirror the three-block JSON-LD the live site serves (Organization, WebSite,
ProfilePage) on a bare alias or account-id URL.  `profile_page_account.html`
is the same page addressed by account id, where `identifier` is the account
id.  `alias_convert.txt` and `stream_id.txt` carry no trailing newline.
