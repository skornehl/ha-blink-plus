# Blink API map (reverse engineered from Blink Android app 60.0, Oct 2026)

Source: `com.immediasemi.android.blink` 60.0 (XAPK), decompiled with jadx. 323 Retrofit endpoint annotations, 60 API interfaces. The full list is in `blink-api-endpoints-full.md`.
Codenames used by the app: **owl** = Mini / Mini 2 / wired cams, **lotus** = Video Doorbell, **catalina/xt** = battery outdoor cams on a sync module, **hawk / snowy / superior / chickadee** = newer models (floodlight, 2K+ lines), **rosie** = pan-tilt mount accessory.

## Hosts
| Host | Purpose |
|---|---|
| `https://rest-{tier}.immedia-semi.com/api/` | Main REST API. `tier` (e.g. `prod`, `u011`, `e006`) comes back from login. Same host blinkpy uses |
| `https://api.{env}oauth.blink.com/` | OAuth (`oauth/token`, password + 2FA header `2fa-code`, `hardware_id` header). Newer builds use authorization code + PKCE; blinkpy ≥0.25 already does this |
| `https://app-snaps.{env}blink.com/` | Snapshot service: `GET blink/snapshots/next/{doorbotId}?extras&after-ms&max-wait-ms` (newer devices) |
| `https://api.{env}blink.com/blink/` | Newer Ring-style services (`device_info/v4`, `evm/v2/timeline`, `duos/v1`) |
| `https://api.{ava_env}amazonvision.com/` | Known Faces / smart video descriptions (Amazon Vision) |

All REST calls: `Authorization: Bearer <access_token>`. `%7Binjected_account_id%7D` and `%7Binjected_client_id%7D` in the paths are replaced by the app with account_id and client_id.
Paths that start with `/accounts/...` (no version) are host-relative in the app. blinkpy calls the equivalents under `/api/v1/accounts/...`; confirm the prefix against live traffic.

## What HA needs: the core endpoints
### State / discovery
- `GET v4/accounts/{acct}/homescreen`: every network, sync module, camera, owl, doorbell and accessory, with battery, temperature, Wi-Fi, armed, thumbnail URL. **One poll returns everything** (blinkpy still calls v3)
- `GET v2/accounts/{acct}/networks/{n}/cameras/{c}/config`, `GET v1/.../owls/{id}/config`, `GET v1/.../doorbells/{id}/config`

### Arm / disarm / snooze
- `POST v1/accounts/{acct}/networks/{n}/state/arm`, `.../state/disarm`
- `POST v1/.../networks/{n}/snooze` body `{"snooze_duration": seconds}` / `.../unsnooze`
- Per device: `.../cameras/{id}/snooze`, `.../owls/{id}/snooze`, `.../doorbells/{id}/snooze` (+ `unsnooze`)
- Motion on/off per camera: `POST /accounts/{acct}/networks/{n}/cameras/{c}/{enable|disable}`; doorbells: `.../doorbells/{id}/enable|disable`

### Snapshot (wakes the camera)
- `POST /accounts/{acct}/networks/{n}/cameras/{c}/thumbnail`
- `POST v1/.../owls/{id}/thumbnail`, `POST v1/.../doorbells/{id}/thumbnail`
- Returns a command → poll `GET /accounts/{acct}/networks/{n}/commands/{cmd}` until complete, then re-read homescreen for the new thumbnail URL

### Live view
| Device | Endpoint |
|---|---|
| Sync-module camera | `POST v6/accounts/{acct}/networks/{n}/cameras/{id}/liveview` |
| Mini / owl | `POST v2/accounts/{acct}/networks/{n}/owls/{id}/liveview` |
| Doorbell | `POST v2/accounts/{acct}/networks/{n}/doorbells/{id}/liveview` |

- Body: `{"intent": "liveview" | "extended_liveview" | "notification", "motion_event_start_time": <iso or null>}` (lowercased `IntentType` enum in the app. `extended_liveview` is the app's "Continue"/restart. `notification` passes the motion event time)
- Response JSON names: `command_id`, `parent_command_id`, `server`, `video_id`, `media_id`, `polling_interval`, `duration`, `continue_interval`, `continue_warning`, `options.poor_connection`, `extended_duration`, `is_mclv`, `type` (`lv` / `elv`), `first_joiner`, **`liveview_token`**
- The app passes `liveview_token` to the native player (`Player.setAuthToken`). The player has a `HAS_STOPPED_DUE_TO_INVALID_TOKEN` state
- Keep alive: poll `GET .../commands/{command_id}` every `polling_interval`, POST `.../commands/{id}/update` to extend, and POST `.../commands/{id}/done` to end
- **IMMIS protocol** (native `libwalnut.so`, `IMMIStreamSource`): TLS TCP connection to `server`, then a 122-byte auth header (magic `00000028`, serial, client_id, `0108`, 64-byte token field, conn_id, trailer `00000001`). Frames have a 9-byte header: type(1) seq(4) len(4). type `0x00` = MPEG-TS video+audio (payload starts with `0x47`), `0x0A` = keepalive (every 10 s), `0x12` = latency stats. blinkpy's `livestream.py` implements this with a 64-byte null token. Putting `liveview_token` in that field is the likely fix if v6 refuses null tokens
- **IMMI message types** (`IMMI_DATA_FLAG` enum, from libwalnut.so DWARF debug info): DATA=0 (MPEG-TS), END=1, STATS=2, LOG=3, JPEG=4, AUDIO=5 (two-way talk upstream), START=6, **STOP=7**, CLOSE=8, KEEPALIVE_CONFIG=9, KEEPALIVE=10, JOIN=11, AUDIO_CONFIG=12, CLIENT_JOIN=13, REQUEST_SHUTDOWN=14, BUTTON_PRESS=15, ROUNDTRIP=16, ROUNDTRIP_ACK=17, MEDIA_LATENCY_STATS=18, ADAPTIVE_PRESENTATION_CONFIG=19, INLINE_LV_CMD=20, ACCESSORY_MSG=21, CV_MOTION=22, SESSION_LV_CMD=23, SESSION_LV_MSG=24
- **Ending a session** (IMMIStreamSource::setConnectionState → Disconnecting): stop keepalives, send `07 00000001 00000000` (STOP, seq 1, empty), then close. Without the STOP, the camera/sync module stays busy until Blink times the session out ("system busy" in the app)
- Sync Module does one job at a time: run only one live view per network, don't poll during live view, and don't read the USB manifest (`local_storage/manifest/request`) in the background (it blocks the app's clip browser)
- Two-way talk: the native player encodes mic audio as AAC-LC and sends it upstream over IMMIS (`requestMicrophone`, `HasTwoWayAudio`). The working sequence (from [zperkd/blink-plus](https://github.com/zperkd/blink-plus), verified on a Mini): `liveview_token` in the auth header's token field, the initial keepalive `0A 00000000 00000000` right after the header, speaker on = `17 00000003 00000000` (SESSION_LV_CMD, control ID 3), wait for a fresh `18` (SESSION_LV_MSG), then one ADTS AAC-LC 16 kHz mono frame per `05` (AUDIO) packet with its own seq from 0, speaker off = `17 00000004 00000000`. With a null token, video works but talk stays silent
- The player also has an `RTSPStreamSource`, so some newer models may return `rtsps://` instead of `immis://`

### Clips / media
- `POST v4/accounts/{acct}/media?start_time&end_time&pagination_key` (body `MediaPostBody`): cloud clips list
- `GET v4/accounts/{acct}/unwatched_media`, `POST v4/.../media/mark_as_viewed`, `/media/delete`, `/media/favorite`
- Local storage (Sync Module 2 USB): `.../sync_modules/{sm}/local_storage/manifest/request` → `.../manifest/{m}/clip/request/{clip}` → `GET .../local_storage/media/{cmd}`

### Doorbell extras
- `POST v1/.../doorbells/{id}/trigger_chime` (test ding), `GET/POST .../doorbells/{id}/chime/{chimeType}/config`
- `POST v1/.../doorbells/{id}/stay_awake`: keep the doorbell awake (for faster live view)
- `GET/POST .../doorbells/{id}/owl_as_chime/list|update`: use Minis as chimes

### Lights (floodlight / Mini 2 / accessories)
- `POST /api/v1/accounts/{acct}/networks/{n}/cameras/{c}/lights/{on|off}`
- `POST v1/.../owls/{id}/lights/{lightControl}`, `POST v2/.../cameras/{c}/light_accessories/{acc}/lights/{lightControl}`

## Push notifications (real-time motion + doorbell press)
The app uses **Firebase Cloud Messaging**:
- Project `blink-6c1ea`, sender id `351117235468`, app id `1:351117235468:android:14fb3ef63df2a7c2`, API key `AIzaSyBcgwrpglzCZJPUNNeQQnpBbJAFlas_XAI`
- Register the token with Blink: `POST /clients/{client_id}/update` body `{"notification_key": <fcm token>, "app_version", "client_name", "os_version"}`. client_id comes from login
- Payload keys (from `ProcessNotification`): `type` (`doorbell`, `owl`, `chime`, `hawk`, `snowy`, `superior`…), `category`, `event_type`, `detection_type` (`motion` / `person` / `vehicle`), `network`, `network_id`, `camera`, `camera_name`, `target_id`, `server_target_id`, `target_name`, `device_id`, `device_type`, `created_at`, `estimated_clip_length`, `early_command_id`, `smart_video_description`, `temperature`, `temp_alert_type`, `sync_module_id`, `storage_type`, `return_receipt`, `unique_id`, `ring_device_id`, plus `ding-` prefixed keys
- Acknowledge: `POST v2/notification` `{"transaction_id", "status"}` when `return_receipt` is set
- Python receiver: the same approach HA's Ring integration uses (`firebase-messaging` package). Needs testing to confirm Blink accepts a token from a non-Android client

## Integration plan (ha-blink-plus)
1. Custom component built on **blinkpy** (auth, 2FA, token refresh are already solved) plus our own client for the extra endpoints above
2. **Camera entities with `stream_source()`**: start live view on demand → local IMMIS→MPEG-TS proxy (`tcp://127.0.0.1:port`) → HA stream / go2rtc (WebRTC, HLS)
3. **Push listener** (FCM) → `event.blink_doorbell` (ding), binary sensors for motion / person / vehicle, `blink_event` bus events → automations + HA mobile notifications with snapshot
4. Services: snapshot, snooze / unsnooze (system and per camera), trigger chime, lights, clip download, live view stop
5. **Apple Home / Apple TV**: HA HomeKit Bridge in accessory mode per camera; doorbells get `linked_doorbell_sensor` = the ding event, so Apple TV shows the doorbell picture-in-picture. HomeKit snapshots should use the cached thumbnail (don't wake the camera). HKSV is not supported through HA

## Caveats
- Battery cameras wake per stream: 5–10 s startup, limited session length (`session_duration` / continue button), battery drain. Use streams on demand, not 24/7
- Blink cloud only. There is no local stream on battery cameras
- Uses an unofficial API, so Blink can change it at any time (they did with OAuth in 2025)

## Implementation status (ha-blink-plus 0.1.0, Oct 2026)
- Live view built as `custom_components/blink_plus` (fork of HA core blink plus `immis.py`). One persistent local TCP server per camera that serves MPEG-TS; a Blink session starts only while a client is connected
- Token mode `auto`: try `liveview_token` in the 64-byte field, fall back to zeros and remember what worked
- Tested offline against a fake IMMIS relay with ffmpeg (H.264 decoded across session hand-offs). **Not yet tested against the real Blink cloud**
- Next: FCM push (doorbell ding event → HomeKit linked_doorbell_sensor), then snooze / chime / lights
