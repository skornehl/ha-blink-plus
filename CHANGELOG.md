# Changelog

All notable changes to Blink Plus. The project follows [Semantic Versioning](https://semver.org/). It's pre-1.0, so options and behaviour may still change between minor versions.

## [Unreleased]

### Fixed
- Clip archive: a clip that keeps failing no longer blocks the archive. After 3 failed runs it is **skipped for good** (shown as `skipped_clips` on the Last archived clip sensor; `blink_plus.reset_archive` forgets it). Clips that failed before are tried last, so a few broken old clips can't keep newer ones from being copied, and the first full copy can complete.
- Clip archive: a failed **cloud** clip is now counted and retried like a USB clip. Before, the first full copy could be marked complete with cloud clips missing.
- Clip archive: auto-resume no longer starts a new series after giving up, until a run makes progress again. An account with no clip source at all reports the error once instead of retrying every 5 minutes.
- Clip archive: unloading or reloading the entry stops the archiver. Before, a run still in progress could schedule a resume on the old instance.
- Clip archive: a new run cancels a pending resume, and `next_auto_resume_s` is cleared when nothing is scheduled.
- Clip archive: the clear "can't write to <folder>" message is no longer overwritten by the raw exception, and a failed refresh of the USB clip list no longer ends the whole run.
- If setup fails after the live view servers started, their ports are freed, so the retry can bind them again.
- A network error during a poll is a normal failed refresh (no "unexpected error" traceback).
- The OAuth login hands its redirect-only connection back to the pool.
- Live view: an IMMIS frame larger than 4 MB (stream out of sync) ends the session instead of being buffered.
- Blink Plus no longer fails to load when a Home Assistant update changes blinkpy's internals. The load reductions (see *Being nice to your Sync Module*) replace four blinkpy methods; a renamed class or method used to stop the whole integration at setup. Now each one is optional: if it can't be applied, Blink Plus logs a warning and keeps working with blinkpy's normal behaviour for that part.

### Added
- Diagnostics list which load reductions are active (`blinkpy_load_reductions`).

## [0.3.2] - 2026-10-22

### Fixed
- The clip archive no longer stops after a few clips:
  - one clip that fails (sync module still busy, an expired clip list, a hiccup writing to the PC share) is skipped for now, and the run carries on
  - after 2 failures in a row, a fresh clip list is fetched; after 5 in a row the run stops with a clear error
  - runs **resume automatically**: 2 min after a live view interrupted them, 5 min after an error, right away when more clips are waiting. No more pressing the button again.
  - the first full copy only counts as complete once every clip is copied; failed clips are retried
- The **Last archived clip** sensor shows why a clip failed (`last_error`) and when the next automatic resume is (`next_auto_resume_s`)

## [0.3.1] - 2026-10-22

### Changed
- **The first run into a folder copies everything** on the USB drive and in the cloud: no look-back, no per-run cap. After that only new clips are copied. An interrupted first copy (e.g. by a live view) carries on next run.
- The "already copied" memory is now kept **per folder**. Pointing the archive at a new folder starts fresh, and 0.3.0's single list no longer blocks copies.

### Added
- `blink_plus.reset_archive` action: forget what was copied to the current folder, so the next run copies everything again

## [0.3.0] - 2026-10-22

### Added
- **Clip archive**: copies your Blink clips to a folder automatically, e.g. a shared folder on your PC added to Home Assistant as network storage. It works **with or without a Blink subscription**:
  - **Cloud clips** (subscription): downloaded straight from Blink's cloud. This never touches the sync module, so it also runs during live view.
  - **USB clips** (Sync Module 2 local storage, no plan needed): copied one at a time, with pauses in between. It never runs during a live view and pauses as soon as one starts.
  - Files are named `<folder>/<Camera>/<YYYY-MM-DD>/<Camera>_<YYYY-MM-DD_HH-MM-SS>.mp4` (local time)
  - It remembers which clips were already copied, even across restarts, so nothing is copied twice
  - Options: folder, interval (or manual only), look-back hours
  - **Archive clips now** button, **Last archived clip** sensor (status, counts, last path, errors), `blink_plus.archive_clips` action (with a response), and a `blink_plus_clip_saved` event for automations
- Live view waits (up to 45 s) for a USB clip upload that's already running, instead of colliding with it

## [0.2.0] - 2026-10-17
First public release.

### Added
- **Live view** for every Blink camera type: Outdoor/Indoor (sync module), Mini (owl) and Video Doorbell (lotus). It works in dashboards, Apple Home / Apple TV (HomeKit Bridge), go2rtc, Frigate and VLC.
- Built-in IMMIS client, reverse engineered from the Blink Android app 60.0:
  - v6 / v2 live view endpoints with the new `liveview_token`, plus an automatic fallback to the zero token
  - seamless `extended_liveview` hand-off when Blink's session time limit ends a session
  - ends sessions exactly like the app (IMMIS `STOP`, then closing the command), so the camera and sync module are released straight away
  - RTSPS fallback for firmware that hands out `rtsps://` URLs
- Local HTTP MPEG-TS feed per camera: `http://<host>:<port>/stream.ts`, with an optional fixed port range
- Its own OAuth v2 + PKCE login that handles Blink's 2026 changes: per-install UUID `hardware_id`, 2FA via HTTP 202 or 412, a separate cookie jar, OAuth v2 token refresh, and clear error messages (wrong password, wrong code, rate limited, failing step)
- Brand icon and logo (Home Assistant 2026.3+ shows them from the integration folder)
- Diagnostics with a log of the last 150 Blink API calls
- `blink_plus.stop_live_view` action
- Camera attributes: `live_view_state`, `live_view_last_error`, `live_view_cooldown_s`, `live_view_viewers`, `live_view_url`
- Options: listen address, base port, max minutes, linger, token mode, USB clip-list polling

### Sync module protection
- One live view per Blink system at a time. Opening another camera hands the sync module over, like the app does.
- Failed live views are never retried automatically: one attempt, a clean stop, then a 2-minute cool-down
- 60 s rest after the max-minutes limit, so a player that keeps reconnecting can't hold a session open
- No polling while a live view is running
- No `network/update` sync module command and no per-camera `signals` call on each poll (the same data comes from the cloud homescreen)
- No upload of every new USB clip to the cloud
- The USB clip list (Sync Module 2 local storage) is **not** read by default, because reading it gives "system busy" in the Blink app
- Setup finishes immediately; the first full refresh runs in the background, capped at 240 s

### Based on
- Home Assistant's built-in `blink` integration (Apache-2.0) and [blinkpy](https://github.com/fronzbot/blinkpy)

## 0.1.0 – 0.1.8 (private test builds, October 2026)
Development builds tested on a real setup: 2 battery doorbells, 2 outdoor cameras, Sync Module 2 with USB storage, no subscription.
