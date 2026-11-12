<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.png">
    <img src="docs/images/logo.png" alt="Blink Plus" width="520">
  </picture>
</p>

<p align="center">
  <b>Live view for Blink cameras and doorbells in Home Assistant, Apple Home, go2rtc and Frigate.</b><br>
  Built on top of the official Blink integration, with the one thing it can't do: watching your cameras live.
</p>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5?style=for-the-badge&logo=homeassistantcommunitystore&logoColor=white" alt="HACS Custom"></a>
  <a href="https://github.com/akramercury/ha-blink-plus/releases"><img src="https://img.shields.io/github/v/release/akramercury/ha-blink-plus?style=for-the-badge&include_prereleases&color=6366F1" alt="Release"></a>
  <a href="https://github.com/akramercury/ha-blink-plus/actions/workflows/validate.yml"><img src="https://img.shields.io/github/actions/workflow/status/akramercury/ha-blink-plus/validate.yml?style=for-the-badge&label=checks" alt="Checks"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2025.1%2B-18BCF2?style=for-the-badge&logo=homeassistant&logoColor=white" alt="Home Assistant 2025.1+">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/akramercury/ha-blink-plus?style=for-the-badge&color=9333EA" alt="License"></a>
</p>

---

## Why

Home Assistant's built-in Blink integration says it plainly: *"This integration does NOT allow for live viewing of your Blink camera."* Blink Plus adds live view, and keeps everything else (login, sensors, arm/disarm, snapshots, clips).

It speaks Blink's own live view protocol (**IMMIS**), reverse engineered from the current Blink Android app. It turns each camera into a plain MPEG-TS stream that Home Assistant, HomeKit, go2rtc, Frigate and VLC can all play.

## Features

| | |
|---|---|
| 🎥 **Live view** | Outdoor / Indoor (Sync Module), Mini and Video Doorbell, including the new v6 live view API and `liveview_token` |
| 🍎 **Apple Home & Apple TV** | Through HA's HomeKit Bridge. Doorbells can pop up picture-in-picture on Apple TV |
| 🗣️ **Two-way talk** | Speak through the camera while its live view is open: play an audio file with an action, or stream audio to an authenticated endpoint |
| 🔁 **Seamless sessions** | Blink ends live views after a short time limit. Blink Plus continues with `extended_liveview`, like tapping *Continue* in the app |
| 🧠 **Sync-module friendly** | One live view per system, no polling during live view, no background USB scans, never retries a failing camera. The Blink app keeps working |
| 🔐 **Login that works in 2026** | OAuth v2 + PKCE, 2FA (email/SMS), per-install device ID, token refresh, and clear error messages |
| 📊 **Everything from the built-in integration** | Alarm panel (arm/disarm), motion, battery, temperature, Wi-Fi, motion detection switch, snapshots, clip saving |
| 💾 **Clip archive** | Copies clips to a folder or straight onto your PC, **with or without a Blink plan** (cloud clips or Sync Module USB clips) |
| 🩺 **Diagnostics** | Per-camera live view state and the reason for any failure, plus a log of the last 150 Blink API calls |
| 🖼️ **Proper branding** | Icon and logo show in Home Assistant 2026.3+ |

## How it works

```mermaid
flowchart LR
    A["HA dashboard · Apple TV<br/>go2rtc · Frigate · VLC"] -- "http://host:port/stream.ts<br/>(MPEG-TS, H.264 + AAC)" --> B["Blink Plus<br/>live view server<br/>(one per camera)"]
    B -- "POST …/liveview<br/>immis:// + token" --> C["Blink cloud<br/>IMMIS relay"]
    C <--> D["Your camera<br/>(woken on demand)"]
```

* **Nothing runs while nobody is watching**, so idle cameras use no battery and don't touch the sync module.
* When the first viewer connects, the camera wakes up (5–10 s for battery cameras). Several viewers share **one** Blink session.
* When the last viewer leaves, the session is ended **the same way the Blink app ends it** (IMMIS `STOP`, then closing the command), so the camera and sync module are released right away.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=akramercury&repository=ha-blink-plus&category=integration)

Or by hand:
1. HACS → ⋮ → **Custom repositories**
2. Repository: `https://github.com/akramercury/ha-blink-plus`, type **Integration** → Add
3. Search for **Blink Plus** → Download → **restart Home Assistant**

### Manual

1. Download the latest release and copy `custom_components/blink_plus` to `/config/custom_components/blink_plus`
2. Restart Home Assistant

### Set up

[![Open your Home Assistant instance and start setting up Blink Plus.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=blink_plus)

**Settings → Devices & services → Add integration → Blink Plus**
1. Sign in with the **email address** and password you use in the Blink app
2. Enter the 2FA code Blink emails or texts you
3. Each camera now appears as `camera.<name>` with streaming. Open it and live view starts.

> [!TIP]
> Blink Plus replaces the built-in **Blink** integration. Running both works, but you'll get duplicate entities, and the built-in one keeps polling your sync module. Once Blink Plus works, remove the built-in one.

## Configuration

**Settings → Devices & services → Blink Plus → Configure**

| Option | Default | What it does |
|---|---|---|
| Scan interval | `300` s | How often sensors and thumbnails refresh (from Blink's cloud, not the sync module) |
| Live view listen address | `127.0.0.1` | Set `0.0.0.0` if go2rtc / Frigate / VLC run on **another** machine |
| First live view TCP port | `0` (random) | e.g. `18550` gives fixed ports: cameras are sorted by serial and get 18550, 18551, … |
| Max live view minutes | `5` | Per viewing. Protects batteries if a tab is left open. `0` = unlimited |
| Linger | `5` s | Keeps the session this long after the last viewer leaves (quick re-open) |
| Live view token mode | `auto` | `auto` uses the app's `liveview_token` and falls back to zeros. Change it only if support asks you to |
| USB clip list check (minutes) | `0` (off) | Reads the Sync Module 2 USB clip list for motion. **Any value can make the Blink app show "system busy"** while it runs |

## Clip archive: copy your videos to your PC

Blink Plus can copy every new clip into a folder on its own. Point that folder at a share on your PC, and your Blink videos land on your computer automatically, organised by camera and day:

```
Blink/
├── Front Door/
│   └── 2026-10-22/
│       ├── Front Door_2026-10-22_08-14-03.mp4
│       └── Front Door_2026-10-22_17-42-51.mp4
└── Driveway/
    └── 2026-10-22/
        └── Driveway_2026-10-22_12-05-37.mp4
```

| Your setup | Where clips come from | Effect on the sync module |
|---|---|---|
| **Blink subscription** | Blink's cloud | None. It even runs during live view |
| **No subscription**, Sync Module 2 + USB stick | The USB stick (the sync module uploads each clip, then Blink Plus downloads it) | One clip at a time, with pauses. Never during live view; it stops if you open one |
| Both | Both, automatically | |

### 1. Share a folder from your PC
*Windows:* create a folder (e.g. `D:\Blink`) → right-click → **Properties → Sharing → Advanced Sharing** → share it as `Blink` and give your Windows user *Change* permission.

### 2. Add it to Home Assistant
**Settings → System → Storage → Add network storage**
* Name: `blink` · Usage: **Media** · Server: your PC's IP · Protocol: **Samba/Windows (CIFS)**
* Remote share: `Blink` · your Windows username and password

The share now appears inside Home Assistant as `/media/blink`.

> Network storage needs Home Assistant OS or Supervised. On other installs, use any folder under `/media` (or one listed in `allowlist_external_dirs`) and reach it however suits you, e.g. the Samba add-on.

### 3. Turn on the archive
**Settings → Devices & services → Blink Plus → Configure**

| Option | Example | |
|---|---|---|
| Copy USB clips to this folder | `/media/blink` | Empty = archive off |
| Copy new clips every N minutes | `60` | `0` = only with the button or action |
| Only copy clips from the last N hours | `24` | For runs after the first one (the first run into a folder always copies everything). `0` = no limit |

Then press **Archive clips now** (on the *Blink Plus* device) for a first run, or wait for the schedule. **The first run into a folder copies everything** on the USB stick and in the cloud. After that only new clips are copied. Changed folders? The new folder gets its own full copy. To copy everything again into the same folder, run `blink_plus.reset_archive`. The **Last archived clip** sensor shows progress, counts, the last file and any error.

**When a clip can't be copied** (sync module busy, clip list expired, Blink returns an error, the folder isn't writable), the run skips it and carries on. Runs resume by themselves: 2 min after a live view interrupted them, 5 min after an error, 30 s later when more clips are waiting. A clip that fails is retried in the next runs, after the clips that haven't failed yet. After **3 failed runs** it is skipped for good, so one broken clip can't hold up the rest (or keep the first full copy from finishing). `blink_plus.reset_archive` forgets skipped clips too. If no run makes progress after 12 automatic resumes, it waits for the next scheduled run. An account with neither cloud clips nor USB storage reports that once and isn't retried.

| *Last archived clip* attribute | Meaning |
|---|---|
| `status` | `idle`, `running`, `running (first full copy)` |
| `last_run` / `last_result` | When the last run ended and what it did (e.g. `done (cloud: 2 new)`, `paused: a live view started`, `error`) |
| `saved_last_run` / `saved_total` | Clips copied in the last run / into this folder overall |
| `last_clip_camera` / `last_clip_path` | Camera and file of the newest copied clip |
| `last_error` | Why the last clip or run failed, e.g. `clip 123: download HTTP 500` or `can't write to /media/blink/...` |
| `next_auto_resume_s` | Seconds until the next automatic resume, empty if none is scheduled |
| `skipped_clips` | Clips skipped for good after 3 failed runs |

> [!TIP]
> No subscription? Copying from the USB stick uses the sync module, so the Blink app may briefly say *"system busy"* while a clip is being copied. Running every 60 minutes (or overnight with an automation calling `blink_plus.archive_clips`) keeps that out of your way.

**Automation idea:** get a phone notification with the camera name whenever a clip lands on your PC:

```yaml
automation:
  - alias: Blink clip saved
    triggers:
      - trigger: event
        event_type: blink_plus_clip_saved
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "New {{ trigger.event.data.camera }} clip saved"
```

## Apple Home / Apple TV

Add each camera to HA's **HomeKit Bridge** in *accessory mode* (required for cameras):

```yaml
homekit:
  - name: Front Doorbell
    port: 21065
    mode: accessory
    filter:
      include_entities:
        - camera.front_door
    entity_config:
      camera.front_door:
        video_codec: copy          # Blink already sends H.264
        support_audio: true
        linked_doorbell_sensor: binary_sensor.front_door_motion
        linked_motion_sensor: binary_sensor.front_door_motion

  - name: Driveway
    port: 21066
    mode: accessory
    filter:
      include_entities:
        - camera.driveway
    entity_config:
      camera.driveway:
        video_codec: copy
        support_audio: true
```

Restart, pair each accessory with the code from the HA notification (Home app → **+** → Add Accessory → More options), and turn on camera/doorbell notifications on the Apple TV (Settings → AirPlay and HomeKit).

* With `linked_doorbell_sensor`, the Apple TV shows the doorbell picture-in-picture when it triggers.
* Black tile in the Home app? Use `video_codec: libx264` instead of `copy`.
* HomeKit Secure Video isn't available through HA's HomeKit Bridge.
* Home app snapshots use the last Blink thumbnail. Blink Plus never wakes a camera just to refresh a tile.

> [!NOTE]
> HA's built-in HomeKit Bridge (UI-configured) can hold at most 150 accessories. If you see *"would exceed the 150 device limit"*, filter that bridge so it doesn't also include the Blink Plus entities.

## Two-way talk

Talk works like in the Blink app: **only while the camera's live view is open** (someone is watching it in HA, HomeKit, go2rtc …, or `blink_plus.start_live_view` opened it). It rides that live view session; Blink Plus never wakes a camera on its own just to talk. One person talks to a camera at a time, and one talk ends after **60 seconds**.

**Open the live view first** with `blink_plus.start_live_view`. It connects to the camera's live view like a player and keeps it open for `seconds` (default 60, at most the *live view time limit*), or until `blink_plus.stop_live_view`. Calling it again extends that time; there is one such hold per camera. It returns per camera `state`, `holding`, `seconds` left, `cooldown_s` and `last_error`. If `cooldown_s` is above 0, the camera is in cool-down (another camera on the same sync module just took over, it hit the time limit, or the last try failed) and nothing was opened; try again after that many seconds.

```yaml
# doorbell pressed: wake the camera, wait until it streams, then speak
- action: blink_plus.start_live_view
  target:
    entity_id: camera.front_door
  data:
    seconds: 90
- wait_template: "{{ is_state_attr('camera.front_door', 'live_view_state', 'streaming') }}"
  timeout: 40
  continue_on_timeout: false
- action: blink_plus.play_audio
  target:
    entity_id: camera.front_door
  data:
    file_path: /media/sounds/be_right_there.mp3
```

**Play a sound or a recorded message** (any format ffmpeg reads, from an [allowed folder](https://www.home-assistant.io/integrations/homeassistant/#allowlist_external_dirs) such as `/media`):

```yaml
action: blink_plus.play_audio
target:
  entity_id: camera.front_door
data:
  file_path: /media/sounds/be_right_there.mp3
```

**Talk from a dashboard** with the bundled card. It shows the live view with sound and a push-to-talk button (walkie-talkie style: hold, speak, release; the visitor hears you about a second later). It is loaded automatically, no resource needed:

```yaml
type: custom:blink-plus-talk-card
title: Front door
cameras:
  - entity: camera.front_door
    name: Front door
  - camera.driveway
```

- Press **Live with sound** to open the live view (it doesn't start on its own, so opening the dashboard never wakes a camera). Unmute with the speaker icon in the picture.
- The card wakes the camera with `start_live_view` first and attaches the player only once the camera streams; a battery camera needs 5–16 s to wake, and a player that starts cold often gives up before the stream exists. While the card shows the live view it keeps renewing that hold; **Hang up** ends it.
- If the camera is in cool-down, the card says so (*Camera busy (another live view on the same sync module), retrying in N s*, or why the last try failed) and tries again by itself when the cool-down is over, for up to 40 s. If the player shows no picture 15 s after it was attached, the card recreates it once.
- The live view plays inside the dashboard (hls.js or WebRTC), also in the Companion app, so the card can tell whether a picture arrived. `exoplayer: true` in the card config uses the Companion app's native Android player (ExoPlayer) instead.
- Browsers only allow the microphone in a **secure context**: open Home Assistant over `https://` (in the Companion app, use the https address at home too). Over plain `http://` the card still shows the live view with sound, but can't record.
- For a doorbell notification, link to the dashboard view with `?camera=camera.front_door&live=1`: the card picks that camera and starts the live view right away.
- The recording goes to the endpoint below, so the user needs control permission for the camera, not admin rights.

<details>
<summary><b>Example: the doorbell rings, you tap the notification and talk</b></summary>

A dashboard *Intercom* (URL `dashboard-intercom`) with a view `door` that holds the card:

```yaml
views:
  - title: Door
    path: door
    cards:
      - type: custom:blink-plus-talk-card
        title: Front door
        cameras:
          - entity: camera.front_door
            name: Front door
          - entity: camera.yard
            name: Yard
```

An automation that wakes the doorbell camera when it rings and sends a notification that opens this view:

```yaml
alias: Doorbell – talk card
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_motion  # your doorbell sensor
    to: "on"
actions:
  # Optional: wake the camera now, so the live view is already running when
  # you tap the notification (the card would otherwise wait 5–16 s for it).
  # It ends after 90 s if nobody opens it.
  - action: blink_plus.start_live_view
    target:
      entity_id: camera.front_door
    data:
      seconds: 90
  - action: notify.mobile_app_my_phone
    data:
      title: Doorbell
      message: Someone is at the front door
      data:
        image: /api/camera_proxy/camera.front_door
        url: /dashboard-intercom/door?camera=camera.front_door&live=1          # iOS
        clickAction: /dashboard-intercom/door?camera=camera.front_door&live=1  # Android
```

Tap the notification: the card opens on the front door and starts the live view with sound. Unmute with the speaker icon if needed, hold **Hold to talk**, speak, release. **Hang up** ends the live view.
</details>

**Talk card in the Companion app.** At home, the app switches to its *internal URL* (often `http://homeassistant.local:8123`); on the road it uses the external one. Two things follow from that:

- **Talking needs https on both addresses.** The Android and iOS web views only allow the microphone in a secure context. Over an `http://` internal URL you can watch and listen, but the talk button can't record.
- **The card can be missing on just one of the two addresses** (*Custom element does not exist: blink-plus-talk-card*). The card is loaded together with the Home Assistant page, and the app keeps its own cached page per address. One cached from before Blink Plus was installed or updated doesn't know the card yet. See [Troubleshooting](#troubleshooting).

So give the app an https address for home too; how you set that up is up to you. The quickest way: in the app, *Settings → Companion app → (your server)*, clear the internal URL, so the app uses its external https address everywhere, then *Troubleshooting → Reset frontend cache*.

**Send audio** to `POST /api/blink_plus/talk/<camera entity id>`. It needs a Home Assistant login (e.g. a [long-lived access token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token)) whose user may control that camera.

- **ADTS AAC (AAC-LC, 16 kHz, mono)** is played as it arrives, so it can be a live stream.
- **Any other format** ffmpeg reads (WebM/Opus from a browser recording, MP3, WAV, M4A …) is read completely first (max. 8 MB, 60 s) and then transcoded.

```bash
ffmpeg -i message.wav -ac 1 -ar 16000 -c:a aac -b:a 32k -f adts - | \
  curl -H "Authorization: Bearer $HA_TOKEN" --data-binary @- \
    http://homeassistant.local:8123/api/blink_plus/talk/camera.front_door
```

| Answer | Meaning |
|---|---|
| `200` `{"frames": …, "seconds": …}` | Played |
| `400` | Not readable as audio (or ADTS that isn't AAC-LC 16 kHz mono), empty, larger than 8 MB, or the upload broke off |
| `403` / `404` | No permission for that camera / not a Blink Plus camera |
| `408` | Longer than 60 seconds (the rest was cut off) |
| `409` | Someone else is talking to this camera |
| `503` | No open live view, the camera didn't acknowledge, or the live view ended |

> [!NOTE]
> Talk needs the live view token (*Live view auth token*: `auto` or `token`). The talk sequence comes from [zperkd/blink-plus](https://github.com/zperkd/blink-plus), where it was verified on a Blink Mini. Other models are untested; please report how it goes. While talking, the camera's `live_view_talking` attribute is `true`.

## go2rtc · Frigate · VLC

Set **Live view listen address** to `0.0.0.0` and a fixed **First live view TCP port** (e.g. `18550`). Each camera's URL is in its `live_view_url` attribute.

```yaml
# go2rtc.yaml
streams:
  front_door: http://<ha-ip>:18550/stream.ts
```

```bash
vlc http://<ha-ip>:18550/stream.ts
```

> [!WARNING]
> Don't point Frigate *detection* at battery cameras. A 24/7 stream drains a Blink battery in hours. Use them for on-demand viewing only.

## Being nice to your Sync Module

A Blink Sync Module can only do **one job at a time**. If it's busy, the Blink app shows *"system busy"*. Blink Plus is built around that:

| Situation | What Blink Plus does |
|---|---|
| You open a second camera on the same system | Ends the first live view (and HA's stream for it), then starts the new one. Never two at once |
| A camera doesn't send video (asleep, low battery, weak Wi-Fi) or Blink refuses | **One** attempt, a clean stop, then a 2-minute cool-down (`live_view_cooldown_s`). HA's automatic reconnects send nothing to Blink |
| A viewer stays open past *Max live view minutes* | Stops, then rests for 60 s so a player that keeps reconnecting can't restart it |
| A live view is running | Background polling is paused |
| Regular polling | Reads Blink's cloud homescreen only. No sync module commands, no per-camera signal requests, no USB clip uploads |
| USB clip list | Not read unless you enable it |

The polling savings come from replacing four blinkpy methods, only for Blink Plus's own connection (the built-in Blink integration is not affected). blinkpy ships with Home Assistant, so an HA update can change it. If one of these methods is gone, Blink Plus logs a warning like *"blinkpy has no … Blink Plus keeps working without this load reduction"* and uses blinkpy's normal behaviour for that part: everything still works, the sync module just gets a little more traffic. Diagnostics show which reductions are active (`blinkpy_load_reductions`).

After you close a camera in HA, it's released within about **35 seconds** (HA keeps its player alive ~30 s, plus the 5 s linger). If the Blink app says busy right after you close a camera, wait that long.

## Entities, actions and attributes

**Entities per camera:** camera (with streaming), motion, battery, temperature, Wi-Fi signal, motion-detection switch. **Per system:** alarm control panel (arm / disarm). **With the clip archive on:** *Archive clips now* button and *Last archived clip* sensor.

| Action | |
|---|---|
| `blink_plus.start_live_view` | Open a camera's live view and keep it open (`seconds`, default 60, at most the live view time limit) so players and talk find it warm; returns state and cool-down. See [Two-way talk](#two-way-talk) |
| `blink_plus.stop_live_view` | End a camera's live view now (also ends `start_live_view`) |
| `blink_plus.play_audio` | Play an audio file through the camera speaker (`file_path`); only while its live view is open, max. 60 s. Runs alongside other camera actions. See [Two-way talk](#two-way-talk) |
| `blink_plus.archive_clips` | Copy new clips to the archive folder now (`hours`, `max_clips`); returns a summary |
| `blink_plus.reset_archive` | Forget what was copied to the current folder; the next run copies everything again |
| `blink_plus.trigger_camera` | Take a new snapshot (wakes the camera) |
| `blink_plus.record` | Record a clip |
| `blink_plus.save_video` | Save the last clip to a file |
| `blink_plus.save_recent_clips` | Save recent clips to a folder |

| Camera attribute | Meaning |
|---|---|
| `live_view_state` | `idle` · `starting` · `streaming` · `error` |
| `live_view_last_error` | Why the last attempt failed (Blink's message, or "camera sent no video …") |
| `live_view_cooldown_s` | Seconds before this camera may start again |
| `live_view_viewers` | Who is connected to the stream right now |
| `live_view_talking` | `true` while someone talks through the camera |
| `live_view_url` | Local MPEG-TS URL for go2rtc / VLC |

## Troubleshooting

<details>
<summary><b>"Custom element does not exist: blink-plus-talk-card"</b></summary>

The card ships with the integration and is loaded with the Home Assistant page, so the page has to be loaded fresh once after installing or updating Blink Plus.

* Browser: reload the page (Ctrl+Shift+R / Cmd+Shift+R).
* Companion app: *Settings → Companion app → Troubleshooting → Reset frontend cache*, or close and reopen the app.
* Only at home in the app? The app uses its internal URL there and has its own cached page for it. See [Talk card in the Companion app](#two-way-talk).
* Please don't add the card as a dashboard resource as a workaround. A resource with `?v=…` stays on that version after the next update, and the old card may stay active. If you added one, remove it once the card loads again.
</details>

<details>
<summary><b>"Invalid authentication" when signing in</b></summary>

* Use the **email address** of your Blink account, not a nickname or username.
* The form now says why login failed: wrong password, wrong 2FA code, **rate limited**, or the Blink step that failed.
* Rate limited? Wait 15–30 minutes before trying again. Every retry resets Blink's timer.
</details>

<details>
<summary><b>The Blink app says "system busy"</b></summary>

1. Make sure the built-in Blink integration is removed. It polls the sync module on its own.
2. Check each camera's `live_view_state` and `live_view_viewers`. If one is still streaming, something (a dashboard card set to live, a forgotten tab) is still watching. Use `camera_view: auto` on dashboard cards.
3. Make sure **USB clip list check** is `0`.
4. Still stuck? Unplug the sync module for 10 seconds, then download diagnostics (see below).
</details>

<details>
<summary><b>A camera doesn't stream</b></summary>

Look at `live_view_last_error`:
* *camera sent no video …* usually means low battery, weak Wi-Fi, or the camera was asleep. Try again after the cool-down.
* A message from Blink means Blink refused the request (e.g. the system is busy or the camera is offline).
* *Unsupported live view transport …*: please open an issue with debug logs.
</details>

<details>
<summary><b>Debug logs and diagnostics</b></summary>

```yaml
logger:
  logs:
    custom_components.blink_plus: debug
```

**Settings → Devices & services → Blink Plus → ⋮ → Download diagnostics** gives each camera's live view state, the last 150 Blink API calls and which blinkpy load reductions are active. Tokens, serials and email are redacted. Please attach it to [bug reports](https://github.com/akramercury/ha-blink-plus/issues/new/choose).
</details>

## Roadmap

- [x] Live view (IMMIS, v6 + token, extended sessions, clean STOP)
- [x] Apple Home / Apple TV via HomeKit Bridge
- [x] Sync-module protection
- [ ] **Push notifications** (Firebase, like the app): instant doorbell press events and motion/person/vehicle detection, without polling
- [x] Clip archive to a folder / PC (cloud and USB)
- [ ] Snooze, chime test, lights, Mini-as-chime
- [x] Two-way talk (during live view: `blink_plus.play_audio`, an authenticated audio endpoint and a push-to-talk dashboard card)
- [x] Open a live view from an automation (`blink_plus.start_live_view`)
- [ ] Two-way talk from the dashboard (microphone button)

## Development

The live view engine and two-way talk have offline tests that run against a fake Blink IMMIS relay and real ffmpeg, so no Blink account is needed. See [`tests/`](tests/). Every push runs them in CI, together with HACS and hassfest validation.

## Credits & disclaimer

* Based on Home Assistant's [`blink`](https://www.home-assistant.io/integrations/blink/) integration (Apache-2.0) and [blinkpy](https://github.com/fronzbot/blinkpy) (MIT). IMMIS framing was first documented by blinkpy and blink-liveview-middleware.
* Endpoint, token and protocol details come from the Blink Android app 60.0 (see [`docs/blink-api-map.md`](docs/blink-api-map.md)).
* The two-way talk sequence and the ADTS handling are adapted from [zperkd/blink-plus](https://github.com/zperkd/blink-plus) (MIT).

**Blink Plus is an independent community project. It isn't affiliated with, endorsed by or sponsored by Blink or Amazon.** "Blink" is a trademark of Amazon.com, Inc. or its affiliates. The project uses an unofficial API that can change at any time. Use it at your own risk.

Licensed under the [Apache License 2.0](LICENSE).
