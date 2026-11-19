"""Copy Blink clips to a folder, with or without a Blink subscription.

Two sources, used automatically:

A. Cloud clips (Blink subscription plan):
   GET /api/v1/accounts/{acct}/media/changed?since=<ts>&page=N
       -> {"media": [{id, created_at, device_name, deleted, media: "/api/...mp4"}]}
   GET <media path> -> MP4. Doesn't touch the sync module, so it also runs
   while a live view is open.

B. USB clips (Sync Module 2 local storage, no plan needed).
   Same endpoints the Blink app uses for local storage:
1. POST .../sync_modules/{sm}/local_storage/manifest/request   -> command id
2. GET  .../sync_modules/{sm}/local_storage/manifest/request/{id}
        -> {"manifest_id": ..., "clips": [{id, camera_name, created_at, size}]}
3. for each new clip:
   POST .../local_storage/manifest/{manifest_id}/clip/request/{clip_id}
        (the sync module uploads that clip to Blink's cloud; wait for the command)
   GET  same URL -> MP4 bytes -> saved to
        <folder>/<camera>/<YYYY-MM-DD>/<camera>_<HH-MM-SS>.mp4

Every step occupies the sync module, so the archiver:
* runs one clip at a time, with a pause between clips
* never runs while a live view is active, and stops at once if one starts
* remembers which clips it already saved, so they aren't fetched twice
* caps the number of clips per run

C. Deleting old USB clips (option usb_delete_after_days / delete_usb_clips).
   Same endpoint blinkpy uses (LocalStorageMediaItem.delete_video):
   POST .../local_storage/manifest/{manifest_id}/clip/delete/{clip_id}
        -> HTTP 200 = deleted (also gone from the Blink app)
   A clip is only deleted when it is provably archived: the archive memory
   says it was saved, the file is still in the folder and it is at least
   MIN_ARCHIVED_BYTES big. Same pacing as the copy (one clip at a time,
   pause, never during a live view, cap per run).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
import logging
from pathlib import Path
import re
import string
from typing import Any

from blinkpy import api as blink_api

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

EVENT_CLIP_SAVED = "blink_plus_clip_saved"
STORE_VERSION = 1
PAUSE_BETWEEN_CLIPS = 3  # s, gives the sync module (and the Blink app) breathing room
MANIFEST_RETRIES = 6
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT = 120  # s per clip
MAX_REMEMBERED = 5000  # clip ids kept per sync module
MAX_FAILS_IN_A_ROW = 5  # then give up this run (and retry later automatically)
RESUME_AFTER_PAUSE = 120  # s, carry on after a live view interrupted the copy
RESUME_AFTER_ERROR = 300  # s
RESUME_AFTER_CAP = 30  # s, more clips waiting
MAX_AUTO_RESUMES = 12  # in a row without progress
MAX_CLIP_ATTEMPTS = 3  # runs a clip may fail before it is skipped for good

# Deleting archived clips from the USB stick
# A real Blink clip is at least several hundred KB (the shortest clip is a few
# seconds of H.264 video). Error pages, JSON answers and cut-off downloads are a
# few KB at most. Anything smaller than this doesn't count as a safe copy.
MIN_ARCHIVED_BYTES = 50 * 1024
DELETE_RETRIES = 3  # attempts per clip, with backoff (3 s, 6 s)
DELETE_MAX_CLIPS = 50  # delete attempts per run
MAX_DAYS = 3650

CLIP_URL = string.Template(
    "/api/v1/accounts/$account_id/networks/$network_id/sync_modules/$sync_id"
    "/local_storage/manifest/$manifest_id/clip/request/$clip_id"
)
DELETE_URL = string.Template(
    "/api/v1/accounts/$account_id/networks/$network_id/sync_modules/$sync_id"
    "/local_storage/manifest/$manifest_id/clip/delete/$clip_id"
)


def _safe(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", str(name)).strip() or "camera"


class ClipArchiver:
    """Copies new USB clips to a folder, on a schedule or on demand."""

    def __init__(self, hass: HomeAssistant, coordinator, entry_id: str, folder: str) -> None:
        """Initialize."""
        self.hass = hass
        self.coordinator = coordinator
        self.folder = folder.rstrip("/")
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"blink_plus.{entry_id}.archive"
        )
        self._saved: dict[str, list[int]] = {}
        self._all: dict[str, Any] = {}
        self._initial_done = False  # first run into a folder copies everything
        self._lock = asyncio.Lock()
        self.status: dict[str, Any] = {
            "state": "idle",
            "last_run": None,
            "last_result": None,
            "last_clip": None,
            "last_clip_path": None,
            "last_clip_camera": None,
            "saved_last_run": 0,
            "saved_total": 0,
            "last_error": None,
            "next_auto_resume_s": None,
            "deleted_last_run": 0,
            "deleted_total": 0,
            "last_delete_error": None,
        }
        self.listeners: list = []
        self.enabled = True
        # Option usb_delete_after_days: after each run, delete archived USB clips
        # older than this many days (0 = off).
        self.delete_after_days = 0
        # Per sync id: clip id -> file path relative to the folder (USB clips
        # saved from 0.7.0 on), and the clips deleted from the USB stick.
        self._paths: dict[str, dict[str, str]] = {}
        self._deleted: dict[str, list[int]] = {}
        # USB clip lists fetched in the current run, per sync id
        self._manifests: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
        self._run_saved = 0
        self.usb_busy = False  # True while the sync module is uploading a clip for us
        self._clip_error = ""
        self._run_failed = 0
        self._resume_handle = None
        self._auto_resumes = 0
        self._resume_given_up = False
        # clip id -> failed runs, per source ("cloud" or a sync id)
        self._failures: dict[str, dict[str, int]] = {}

    @property
    def blink(self):
        """Return the blinkpy instance."""
        return self.coordinator.api

    async def async_load(self) -> None:
        """Load the clips already copied *to this folder*.

        The memory is kept per folder: pointing the archive at a new folder
        starts fresh, so everything in the look-back window is copied there.
        (0.3.0 kept one list for all folders; that list is left behind.)
        """
        data = await self._store.async_load() or {}
        self._all = dict(data.get("folders", {}))
        if "saved" in data and "folders" not in data:
            self._all["__0.3.0__"] = {"saved": data["saved"], "saved_total": 0}
        mine = self._all.get(self.folder, {})
        self._saved = {k: list(v) for k, v in mine.get("saved", {}).items()}
        self.status["saved_total"] = int(mine.get("saved_total", 0))
        self._initial_done = bool(mine.get("initial_done", False))
        self._failures = {
            k: {str(c): int(n) for c, n in v.items()}
            for k, v in mine.get("failures", {}).items()
        }
        self.status["skipped_clips"] = self._skipped_count()
        self._paths = {
            k: {str(c): str(p) for c, p in v.items()}
            for k, v in mine.get("paths", {}).items()
        }
        self._deleted = {k: list(v) for k, v in mine.get("deleted", {}).items()}
        self.status["deleted_total"] = int(mine.get("deleted_total", 0))

    async def _async_save_store(self) -> None:
        saved = {k: v[-MAX_REMEMBERED:] for k, v in self._saved.items()}
        # Keep file paths only for clips that are still remembered.
        for key, paths in self._paths.items():
            keep = {str(cid) for cid in saved.get(key, [])}
            self._paths[key] = {c: p for c, p in paths.items() if c in keep}
        self._all[self.folder] = {
            "saved": saved,
            "saved_total": self.status["saved_total"],
            "initial_done": self._initial_done,
            "failures": self._failures,
            "paths": self._paths,
            "deleted": {k: v[-MAX_REMEMBERED:] for k, v in self._deleted.items()},
            "deleted_total": self.status["deleted_total"],
        }
        await self._store.async_save({"folders": self._all})

    async def async_reset(self) -> None:
        """Forget what was copied to this folder (next run copies again).

        What was deleted from the USB stick is kept: those clips are gone.
        """
        if self._lock.locked():
            raise RuntimeError("archive is running, try again when it's done")
        self.cancel_resume()
        self._saved = {}
        self._failures = {}
        self._paths = {}
        self._initial_done = False
        self.status.update(saved_total=0, skipped_clips=0, last_clip=None, last_clip_path=None,
                           last_clip_camera=None, last_result="memory reset")
        await self._async_save_store()
        self._notify()

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("archive listener failed", exc_info=True)

    def _live_active(self) -> bool:
        return any(srv.active for srv in self.coordinator.live_servers.values())

    def shutdown(self) -> None:
        """Stop for good (entry unload): no new runs, no pending resume."""
        self.enabled = False
        self.cancel_resume()

    # ------------------------------------------------------- failing clips
    def _fail_count(self, key: str, cid: int) -> int:
        return self._failures.get(key, {}).get(str(cid), 0)

    def _given_up(self, key: str, cid: int) -> bool:
        return self._fail_count(key, cid) >= MAX_CLIP_ATTEMPTS

    def _note_failure(self, key: str, cid: int) -> None:
        """Count a failed run for this clip; after MAX_CLIP_ATTEMPTS it is skipped."""
        counts = self._failures.setdefault(key, {})
        counts[str(cid)] = counts.get(str(cid), 0) + 1
        if counts[str(cid)] == MAX_CLIP_ATTEMPTS:
            _LOGGER.warning(
                "Blink clip archive: giving up on clip %s after %d failed runs",
                cid, MAX_CLIP_ATTEMPTS,
            )
            self.status["skipped_clips"] = self._skipped_count()
        else:
            self._run_failed += 1

    def _note_success(self, key: str, cid: int) -> None:
        self._failures.get(key, {}).pop(str(cid), None)

    def _skipped_count(self) -> int:
        return sum(
            1 for counts in self._failures.values() for n in counts.values()
            if n >= MAX_CLIP_ATTEMPTS
        )

    # ------------------------------------------------------------------ run
    async def async_run(self, hours: float = 24, max_clips: int = 25) -> dict[str, Any]:
        """Copy clips from the last `hours` that haven't been saved yet."""
        if not self.enabled:
            return {"result": "disabled", "error": self.status["last_error"]}
        if self._lock.locked():
            return {"result": "already running"}
        async with self._lock:
            # This run decides about the next resume itself.
            self.cancel_resume()
            self.status.update(state="running", last_error=None, saved_last_run=0)
            self._notify()
            result = "done"
            kind = "done"  # done | more | paused | error | fatal
            self._run_saved = 0
            self._run_failed = 0
            self._manifests = {}
            full_copy = not self._initial_done
            if full_copy:
                # First run into this folder: everything on the drive / in the
                # cloud, no look-back and no per-run cap.
                hours, max_clips = 0, 100_000
                self.status["state"] = "running (first full copy)"
                self._notify()
            try:
                notes = []
                cloud_found = await self._archive_cloud(hours, max_clips)
                if cloud_found is not None:
                    notes.append(f"cloud: {cloud_found} new")
                syncs = self._local_storage_syncs()
                if syncs and self._run_saved < max_clips:
                    if self._live_active():
                        notes.append("USB skipped: live view running")
                    else:
                        for sync in syncs:
                            await self._archive_sync(
                                sync, hours, max_clips - self._run_saved
                            )
                if cloud_found is None and not syncs:
                    raise _NoSource(
                        "no clips source: no cloud clips (subscription) and no "
                        "Sync Module with active USB storage"
                    )
                if self._run_saved >= max_clips:
                    notes.append(f"stopped at {max_clips} clips, more next run")
                    kind = "more"
                if full_copy:
                    notes.insert(0, "first full copy")
                    if self._run_failed:
                        notes.append(f"{self._run_failed} clip(s) to retry")
                        kind = "more"
                    elif any(n.startswith("USB skipped") for n in notes):
                        kind = "more"
                    else:
                        self._initial_done = True
                elif self._run_failed:
                    notes.append(f"{self._run_failed} clip(s) to retry")
                skipped = self.status.get("skipped_clips") or 0
                if skipped:
                    notes.append(f"{skipped} clip(s) skipped after {MAX_CLIP_ATTEMPTS} failed runs")
                result = "done" + (f" ({'; '.join(notes)})" if notes else "")
            except _LiveViewStarted:
                result = "paused: a live view started"
                kind = "paused"
            except _NoSource as err:
                # Nothing to retry: the account has no source of clips.
                _LOGGER.warning("Blink clip archive: %s", err)
                self.status["last_error"] = str(err)
                result = "error"
                kind = "fatal"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Blink clip archive failed: %s", err, exc_info=True)
                self.status["last_error"] = str(err) or type(err).__name__
                result = "error"
                kind = "error"
            finally:
                saved = self._run_saved
                self.status.update(
                    state="idle",
                    last_run=dt_util.utcnow().isoformat(),
                    last_result=result,
                    saved_last_run=saved,
                )
                await self._async_save_store()
                self._notify()
            _LOGGER.info("Blink clip archive: %s, %d clip(s) saved", result, saved)
            summary: dict[str, Any] = {"result": result, "saved": saved}
            if self.delete_after_days > 0 and kind == "done" and self.enabled:
                # Free the USB stick: delete clips that are safely archived.
                # Only after a complete run (not paused, no error, nothing
                # waiting), so the copy always goes first.
                deleted = await self._async_delete_usb(
                    self.delete_after_days, dry_run=False, max_clips=DELETE_MAX_CLIPS
                )
                summary["usb_delete"] = deleted["result"]
                summary["deleted"] = len(deleted["deleted"])
            self._schedule_resume(kind, saved, hours, max_clips)
            return summary

    def _schedule_resume(self, kind: str, saved: int, hours, max_clips) -> None:
        """Carry on by itself after a pause, an error, or the per-run cap."""
        if saved or kind == "done":
            # Progress (or a clean run): auto-resume may be used again.
            self._auto_resumes = 0
            self._resume_given_up = False
        delay = {
            "paused": RESUME_AFTER_PAUSE,
            "error": RESUME_AFTER_ERROR,
            "more": RESUME_AFTER_CAP,
        }.get(kind)
        if delay is None or not self.enabled:
            return
        if self._resume_given_up:
            # Gave up earlier and still no progress: wait for the next scheduled
            # run instead of starting another series of resumes.
            return
        if not saved:
            self._auto_resumes += 1
        if self._auto_resumes > MAX_AUTO_RESUMES:
            _LOGGER.warning("Blink clip archive: giving up auto-resume (no progress)")
            self._resume_given_up = True
            return
        self.cancel_resume()
        self.status["next_auto_resume_s"] = delay
        self._notify()

        async def _resume(_now=None) -> None:
            self._resume_handle = None
            self.status["next_auto_resume_s"] = None
            await self.async_run(hours=hours, max_clips=max_clips)

        self._resume_handle = async_call_later(self.hass, delay, _resume)

    def cancel_resume(self) -> None:
        """Cancel a scheduled automatic resume (unload / reset / new run)."""
        if self._resume_handle:
            self._resume_handle()
            self._resume_handle = None
        self.status["next_auto_resume_s"] = None

    def _local_storage_syncs(self) -> list:
        syncs = []
        for sync in self.blink.sync.values():
            # Only real sync modules (not Minis/doorbells) with an active USB drive
            if getattr(sync, "sync_id", None) and getattr(sync, "local_storage", False):
                syncs.append(sync)
        return syncs

    async def _archive_cloud(self, hours: float, budget: int) -> int | None:
        """Copy cloud clips (subscription). Returns new clips found, None if none exist."""
        since = dt_util.utcnow() - timedelta(hours=hours) if hours else None
        stamp = (since or dt_util.parse_datetime("2015-01-01T00:00:00+00:00")).strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        )
        base = f"{self.blink.urls.base_url}/api/v1/accounts/{self.blink.account_id}"
        items: list[dict[str, Any]] = []
        for page in range(1, 51):
            data = await blink_api.http_get(
                self.blink, f"{base}/media/changed?since={stamp}&page={page}"
            )
            media = data.get("media") if isinstance(data, dict) else None
            if not media:
                break
            items.extend(media)
        clips = [
            m for m in items
            if isinstance(m, dict) and m.get("media") and not m.get("deleted")
            and str(m.get("media")).split("?")[0].endswith(".mp4")
        ]
        if not clips:
            return None
        done = set(self._saved.get("cloud", []))
        todo = []
        for m in clips:
            try:
                cid = int(m["id"])
                created = dt_util.parse_datetime(str(m["created_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if cid in done or created is None or self._given_up("cloud", cid):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt_util.UTC)
            todo.append((created, cid, m.get("device_name") or "camera", m["media"]))
        # Oldest first, but clips that failed before go last.
        todo.sort(key=lambda t: (self._fail_count("cloud", t[1]), t[0]))
        for created, cid, camera, media in todo[: max(budget - self._run_saved, 0)]:
            url = f"{self.blink.urls.base_url}{media}"
            self._clip_error = ""
            try:
                path = await self._fetch_to_file(url, camera, created, cid)
            except Exception as err:  # noqa: BLE001
                self._clip_error = self._clip_error or f"{type(err).__name__}: {err}"
                path = None
            if path is None:
                self._note_failure("cloud", cid)
                self.status["last_error"] = f"cloud clip {cid}: {self._clip_error}"
                self._notify()
                continue
            self._note_success("cloud", cid)
            self._record_saved("cloud", cid, camera, created, path)
            await self._async_save_store()
            await asyncio.sleep(0.5)
        return len(todo)

    def _record_saved(self, key: str, cid: int, camera: str, created, path: Path) -> None:
        self._saved.setdefault(key, []).append(cid)
        if key != "cloud":
            # Remember where the copy is: deleting the USB clip later checks it.
            with contextlib.suppress(ValueError):
                self._paths.setdefault(key, {})[str(cid)] = str(
                    Path(path).relative_to(self.folder)
                )
        self._run_saved += 1
        self.status.update(
            last_clip=created.isoformat(),
            last_clip_path=str(path),
            last_clip_camera=camera,
            saved_total=self.status["saved_total"] + 1,
        )
        self._notify()
        self.hass.bus.async_fire(
            EVENT_CLIP_SAVED,
            {"camera": camera, "path": str(path), "created_at": created.isoformat()},
        )

    async def _manifest(self, sync) -> tuple[Any, list[dict[str, Any]]]:
        base = self.blink.urls.base_url
        prefix = (
            f"{base}/api/v1/accounts/{self.blink.account_id}"
            f"/networks/{sync.network_id}/sync_modules/{sync.sync_id}"
            "/local_storage/manifest/request"
        )
        resp = await blink_api.http_post(self.blink, prefix)
        request_id = resp.get("id") if isinstance(resp, dict) else None
        if not request_id:
            raise RuntimeError(f"USB clip list request refused: {resp}")
        await blink_api.wait_for_command(self.blink, resp)
        for attempt in range(MANIFEST_RETRIES):
            data = await blink_api.http_get(self.blink, f"{prefix}/{request_id}")
            if isinstance(data, dict) and "clips" in data:
                return data.get("manifest_id"), data["clips"]
            await asyncio.sleep(3 * (attempt + 1))
        raise RuntimeError("the Sync Module didn't return the USB clip list")

    async def _usb_manifest(self, sync, refresh: bool = False) -> tuple[Any, list[dict[str, Any]]]:
        """The USB clip list of this run (fetched once, shared by copy and delete)."""
        key = str(sync.sync_id)
        if refresh or key not in self._manifests:
            self.usb_busy = True
            try:
                self._manifests[key] = await self._manifest(sync)
            finally:
                self.usb_busy = False
        return self._manifests[key]

    @staticmethod
    def _camera(sync, clip: dict[str, Any]) -> str:
        names = getattr(sync, "_names_table", {}) or {}
        return names.get(clip.get("camera_name"), clip.get("camera_name", "camera"))

    async def _archive_sync(self, sync, hours: float, budget: int) -> int:
        if budget <= 0:
            return 0
        manifest_id, clips = await self._usb_manifest(sync)
        key = str(sync.sync_id)
        done = set(self._saved.get(key, []))
        cutoff = dt_util.utcnow() - timedelta(hours=hours) if hours else None

        todo = []
        for clip in clips:
            try:
                cid = int(clip["id"])
                created = dt_util.parse_datetime(str(clip["created_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if cid in done or created is None or self._given_up(key, cid):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt_util.UTC)
            if cutoff and created < cutoff:
                continue
            todo.append((created, cid, self._camera(sync, clip)))
        # Oldest first, but clips that failed in earlier runs go last, so a few
        # broken clips at the front can't block newer ones forever.
        todo.sort(key=lambda t: (self._fail_count(key, t[1]), t[0]))
        _LOGGER.debug("%d new USB clip(s) to copy on %s", len(todo), sync.name)

        saved = 0
        fails = 0
        for created, cid, camera in todo[:budget]:
            if self._live_active():
                raise _LiveViewStarted
            self._clip_error = ""
            try:
                path = await self._download(sync, manifest_id, cid, camera, created)
            except Exception as err:  # noqa: BLE001  (one bad clip must not end the run)
                # Keep a clearer reason set further down (e.g. "can't write to ...").
                self._clip_error = self._clip_error or f"{type(err).__name__}: {err}"
                path = None
            if path is None:
                fails += 1
                self._note_failure(key, cid)
                _LOGGER.warning(
                    "Clip %s from %s not copied (%s), %d failed in a row",
                    cid, camera, self._clip_error, fails,
                )
                self.status["last_error"] = f"clip {cid}: {self._clip_error}"
                self._notify()
                if fails >= MAX_FAILS_IN_A_ROW:
                    raise RuntimeError(
                        f"{fails} clips in a row failed, last: {self._clip_error}"
                    )
                if fails == 2:
                    # Blink's clip list can expire during a long copy: get a new one.
                    _LOGGER.debug("Refreshing the USB clip list")
                    try:
                        manifest_id, _ = await self._usb_manifest(sync, refresh=True)
                    except Exception as err:  # noqa: BLE001
                        # Keep going with the old list rather than ending the run.
                        _LOGGER.debug("Could not refresh the USB clip list: %s", err)
                await asyncio.sleep(PAUSE_BETWEEN_CLIPS * 3)
                continue
            fails = 0
            saved += 1
            self._note_success(key, cid)
            self._record_saved(key, cid, camera, created, path)
            await self._async_save_store()
            await asyncio.sleep(PAUSE_BETWEEN_CLIPS)
        return saved

    async def _download(self, sync, manifest_id, clip_id, camera, created) -> Path | None:
        url = self.blink.urls.base_url + CLIP_URL.substitute(
            account_id=self.blink.account_id,
            network_id=sync.network_id,
            sync_id=sync.sync_id,
            manifest_id=manifest_id,
            clip_id=clip_id,
        )
        # Ask the sync module to upload the clip, then wait for it.
        self.usb_busy = True
        try:
            resp = await blink_api.http_post(self.blink, url)
            await blink_api.wait_for_command(self.blink, resp)
        finally:
            self.usb_busy = False

        return await self._fetch_to_file(url, camera, created, clip_id)

    def _clip_paths(self, camera: str, created, clip_id) -> tuple[Path, Path]:
        """Where a clip is saved: <camera>_<time>.mp4, or ..._<clip id>.mp4 if taken."""
        local = dt_util.as_local(created)
        cam = _safe(camera)
        folder = Path(self.folder) / cam / local.strftime("%Y-%m-%d")
        stem = f"{cam}_{local.strftime('%Y-%m-%d_%H-%M-%S')}"
        return folder / f"{stem}.mp4", folder / f"{stem}_{clip_id}.mp4"

    async def _fetch_to_file(self, url: str, camera: str, created, clip_id) -> Path | None:
        path, with_id = self._clip_paths(camera, created, clip_id)
        if await self.hass.async_add_executor_job(path.exists):
            path = with_id
        for attempt in range(DOWNLOAD_RETRIES):
            response = await blink_api.http_get(
                self.blink, url, json=False, timeout=DOWNLOAD_TIMEOUT
            )
            status = getattr(response, "status", None)
            if status == 200:
                data = await response.read()
                if data:
                    try:
                        await self.hass.async_add_executor_job(_write, path, data)
                    except OSError as err:
                        self._clip_error = f"can't write to {path.parent}: {err}"
                        raise
                    _LOGGER.debug("Saved %s (%d KB)", path, len(data) // 1024)
                    return path
                self._clip_error = "empty download"
            else:
                self._clip_error = (
                    f"download HTTP {status}" if status else "no response from Blink"
                )
            await asyncio.sleep(3 * (attempt + 1))
        _LOGGER.warning("Could not download clip %s from %s", clip_id, camera)
        return None

    # ------------------------------------------------------ USB clip delete
    async def async_delete_usb(
        self, older_than_days: int, dry_run: bool = False, max_clips: int = DELETE_MAX_CLIPS
    ) -> dict[str, Any]:
        """Delete archived USB clips older than `older_than_days` (action)."""
        if not self.enabled:
            return {"result": "disabled", "error": self.status["last_error"]}
        if self._lock.locked():
            return {"result": "already running"}
        async with self._lock:
            self._manifests = {}
            return await self._async_delete_usb(older_than_days, dry_run, max_clips)

    async def _async_delete_usb(
        self, older_than_days: int, dry_run: bool, max_clips: int
    ) -> dict[str, Any]:
        """Delete (or with dry_run only list) provably archived old USB clips.

        Runs under the archive lock: copy and delete never overlap.
        """
        report: dict[str, Any] = {
            "result": "done",
            "dry_run": dry_run,
            "older_than_days": older_than_days,
            "deleted": [],
            "skipped": [],
            "errors": [],
            "remaining": 0,
        }
        if not 1 <= int(older_than_days) <= MAX_DAYS:
            report["result"] = f"error: older_than_days must be 1 to {MAX_DAYS}"
            return report
        syncs = self._local_storage_syncs()
        if not syncs:
            report["result"] = "nothing to do: no Sync Module with active USB storage"
            return report
        if self._live_active():
            report["result"] = "skipped: live view running"
            return report
        if not dry_run:
            self.status.update(deleted_last_run=0, last_delete_error=None)
        self.status["state"] = "deleting old USB clips" + (" (dry run)" if dry_run else "")
        self._notify()
        error = None
        try:
            budget = max(int(max_clips), 1)
            for sync in syncs:
                budget -= await self._delete_sync(
                    sync, int(older_than_days), dry_run, budget, report
                )
            verb = "would delete" if dry_run else "deleted"
            report["result"] = (
                f"{'dry run' if dry_run else 'done'}: {verb} {len(report['deleted'])}, "
                f"skipped {len(report['skipped'])} (not safely archived), "
                f"{len(report['errors'])} error(s)"
            )
            if report["remaining"]:
                report["result"] += f", {report['remaining']} more next run"
        except _LiveViewStarted:
            report["result"] = "paused: a live view started"
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Deleting USB clips failed: %s", err, exc_info=True)
            error = str(err) or type(err).__name__
            report["result"] = "error"
            report["error"] = error
        finally:
            self.status["state"] = "idle"
            if not dry_run:
                self.status["deleted_last_run"] = len(report["deleted"])
                if error:
                    self.status["last_delete_error"] = error
                await self._async_save_store()
            self._notify()
        _LOGGER.info("Blink USB clip delete: %s", report["result"])
        return report

    async def _delete_sync(
        self, sync, older_than_days: int, dry_run: bool, budget: int, report: dict[str, Any]
    ) -> int:
        """Delete due clips on one sync module. Returns the delete attempts used."""
        if budget <= 0:
            return 0
        manifest_id, clips = await self._usb_manifest(sync)
        key = str(sync.sync_id)
        saved = set(self._saved.get(key, []))
        gone = set(self._deleted.get(key, []))
        cutoff = dt_util.utcnow() - timedelta(days=older_than_days)

        due = []
        for clip in clips:
            try:
                cid = int(clip["id"])
                created = dt_util.parse_datetime(str(clip["created_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt_util.UTC)
            if created >= cutoff:
                continue  # too young
            if cid in gone:
                # Deleted before (Blink may list it a little longer): never again.
                _LOGGER.debug("USB clip %s was deleted before, not retried", cid)
                continue
            camera = self._camera(sync, clip)
            item = {
                "sync_module": sync.name,
                "clip_id": cid,
                "camera": camera,
                "created_at": created.isoformat(),
            }
            path, reason = await self._archived_file(key, cid, camera, created, saved)
            if path is None:
                report["skipped"].append({**item, "reason": reason})
                _LOGGER.debug("USB clip %s (%s) kept: %s", cid, camera, reason)
                continue
            due.append((created, cid, {**item, "path": str(path)}))
        due.sort(key=lambda t: t[0])  # oldest first
        todo = due[:budget]
        report["remaining"] += len(due) - len(todo)
        if dry_run:
            report["deleted"].extend(item for _, _, item in todo)
            return len(todo)

        used = 0
        fails = 0
        for _created, cid, item in todo:
            if self._live_active():
                raise _LiveViewStarted
            used += 1
            error = await self._delete_clip(sync, manifest_id, cid)
            if error is None:
                fails = 0
                self._deleted.setdefault(key, []).append(cid)
                self._paths.get(key, {}).pop(str(cid), None)
                self.status["deleted_total"] += 1
                report["deleted"].append(item)
                _LOGGER.info(
                    "Deleted USB clip %s (%s, %s), archived as %s",
                    cid, item["camera"], item["created_at"], item["path"],
                )
                await self._async_save_store()
                self._notify()
                await asyncio.sleep(PAUSE_BETWEEN_CLIPS)
                continue
            fails += 1
            report["errors"].append({**item, "error": error})
            self.status["last_delete_error"] = f"clip {cid}: {error}"
            self._notify()
            _LOGGER.warning(
                "USB clip %s from %s not deleted (%s), %d failed in a row",
                cid, item["camera"], error, fails,
            )
            if fails >= MAX_FAILS_IN_A_ROW:
                raise RuntimeError(f"{fails} deletes in a row failed, last: {error}")
            if fails == 2:
                # The clip list may have expired (e.g. after a long copy).
                try:
                    manifest_id, _ = await self._usb_manifest(sync, refresh=True)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not refresh the USB clip list: %s", err)
            await asyncio.sleep(PAUSE_BETWEEN_CLIPS * 3)
        return used

    async def _archived_file(
        self, key: str, cid: int, camera: str, created, saved: set[int]
    ) -> tuple[Path | None, str]:
        """(path, "") if the clip is provably archived, else (None, reason).

        Provably archived = the archive memory says it was saved, the file is
        in the folder, and it is at least MIN_ARCHIVED_BYTES big.
        """
        if cid not in saved:
            fails = self._fail_count(key, cid)
            if fails >= MAX_CLIP_ATTEMPTS:
                return None, f"not archived: copy failed {fails} times, skipped for good"
            if fails:
                return None, f"not archived yet: copy failed {fails} time(s), will retry"
            return None, "not archived: not in the archive memory of this folder"
        recorded = self._paths.get(key, {}).get(str(cid))
        if recorded:
            candidates = [Path(self.folder) / recorded]
        else:
            # Saved before 0.7.0 (no path recorded): the name the archiver
            # gives it. Renamed camera / changed time zone -> not found -> kept.
            plain, with_id = self._clip_paths(camera, created, cid)
            candidates = [with_id, plain]
        path, size = await self.hass.async_add_executor_job(_first_file, candidates)
        if path is None:
            return None, f"archived file missing: {candidates[-1]}"
        if size < MIN_ARCHIVED_BYTES:
            return None, (
                f"archived file too small ({size} bytes, at least "
                f"{MIN_ARCHIVED_BYTES} expected): {path}"
            )
        return path, ""

    async def _delete_clip(self, sync, manifest_id, clip_id) -> str | None:
        """Delete one clip on the USB stick. None if done, else the error."""
        url = self.blink.urls.base_url + DELETE_URL.substitute(
            account_id=self.blink.account_id,
            network_id=sync.network_id,
            sync_id=sync.sync_id,
            manifest_id=manifest_id,
            clip_id=clip_id,
        )
        error = "no response from Blink"
        for attempt in range(DELETE_RETRIES):
            if attempt:
                await asyncio.sleep(3 * 2 ** (attempt - 1))
                if self._live_active():
                    raise _LiveViewStarted
            self.usb_busy = True
            try:
                response = await blink_api.http_post(self.blink, url, json=False)
            except Exception as err:  # noqa: BLE001
                error = f"{type(err).__name__}: {err}"
                continue
            finally:
                self.usb_busy = False
            status = getattr(response, "status", None)
            body = ""
            with contextlib.suppress(Exception):
                body = (await response.text())[:200]
            _LOGGER.debug("Delete USB clip %s -> HTTP %s %s", clip_id, status, body)
            if status == 200:
                return None
            error = f"delete HTTP {status}" if status else "no response from Blink"
        return error


def _first_file(paths: list[Path]) -> tuple[Path | None, int]:
    """The first of `paths` that is a file, and its size."""
    for path in paths:
        try:
            if path.is_file():
                return path, path.stat().st_size
        except OSError:
            continue
    return None, 0


class _LiveViewStarted(Exception):
    """Stop archiving: someone opened a live view."""


class _NoSource(RuntimeError):
    """The account has neither cloud clips nor USB storage (nothing to retry)."""


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
