# Captured tango.me fixtures

Scrubbed copies of live `www.tango.me` responses — the watch capture that
mirrors `/tmp/watch.bin`, and the API contract decoded in `PLAN.md` from
check-in-cookie-jar captures (`curl -c jar.txt -b jar.txt`) — with ids, names,
thumbnails and CDN hashes replaced by the fixed values in `tests/fake_net.py`
(`STREAM_ID`, account id `GGGGHHHHIIIIJJJJKKKKLL`, CDN
`cinema-in41.tango.me`, hashes `'1' * 32` / `'2' * 32` / `'3' * 32`).  No
tokens, account ids or personal data are stored here; `check_in` replies its
cookies through the route table in `base_routes()` instead of the fixture file.

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
| `feed.json` | `POST https://www.tango.me/api/showroom-streams/api/public/web/catalog/v1?traceId=<uuid>` | `{"size":30,"platform":"web_desktop","hMarketingCampaign":false}` |
| `master.m3u8` | `GET https://cinema-in41.tango.me/v2/11111111111111111111111111111111/master.m3u8` | — |
| `hd.m3u8` | `GET https://cinema-in41.tango.me/v2/22222222222222222222222222222222/hd.m3u8` | — |
| `ld.m3u8` | `GET https://cinema-in41.tango.me/v2/33333333333333333333333333333333/ld.m3u8` | — |

`watch.json` mirrors the real watch response captured in `/tmp/watch.bin`
(stream id, account ids, thumbnail and the four CDN hashes scrubbed; viewer
and point counters kept as observed).  `watch_terminated.json` and
`watch_bad_request.json` are the observed `{"code": ...}` shapes recorded in
`PLAN.md` rather than fresh captures: a terminated stream and an audio room
both answer the same watch call, but the capture session predates this fixture
set, so their bodies are reconstructed from the documented failure modes.
`alias_convert.txt` and `stream_id.txt` carry no trailing newline.
