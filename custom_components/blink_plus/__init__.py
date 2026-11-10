"""Support for Blink Home Camera System."""

from copy import deepcopy
from datetime import timedelta
import logging
import time
from typing import Any

from blinkpy.blinkpy import Blink

from homeassistant.components import persistent_notification
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ARCHIVE_FOLDER,
    CONF_ARCHIVE_HOURS,
    CONF_ARCHIVE_INTERVAL,
    CONF_STREAM_BASE_PORT,
    CONF_STREAM_HOST,
    CONF_STREAM_LINGER,
    CONF_STREAM_MAX_MINUTES,
    CONF_STREAM_TOKEN_MODE,
    CONF_USB_MOTION_MINUTES,
    DEFAULT_ARCHIVE_FOLDER,
    DEFAULT_ARCHIVE_HOURS,
    DEFAULT_ARCHIVE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STREAM_BASE_PORT,
    DEFAULT_STREAM_HOST,
    DEFAULT_STREAM_LINGER,
    DEFAULT_STREAM_MAX_MINUTES,
    DEFAULT_STREAM_TOKEN_MODE,
    DEFAULT_USB_MOTION_MINUTES,
    DOMAIN,
    LEGACY_HARDWARE_ID,
    PLATFORMS,
)
from .coordinator import BlinkConfigEntry, BlinkUpdateCoordinator
from .archive import ClipArchiver
from .auth import BlinkPlusAuth
from .immis import BlinkLiveViewServer, live_key
from .session import async_blink_session
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _reauth_flow_wrapper(
    hass: HomeAssistant, entry: BlinkConfigEntry, data: dict[str, Any]
) -> None:
    """Reauth flow wrapper."""
    entry.async_start_reauth(hass, data=data)
    persistent_notification.async_create(
        hass,
        (
            "Blink configuration migrated to a new version. Please go to the"
            " integrations page to re-configure (such as sending a new 2FA key)."
        ),
        "Blink Migration",
    )


async def async_migrate_entry(hass: HomeAssistant, entry: BlinkConfigEntry) -> bool:
    """Handle migration of a previous version config entry."""
    _LOGGER.debug("Migrating from version %s", entry.version)
    data = {**entry.data}
    if entry.version == 1:
        data.pop("login_response", None)
        await _reauth_flow_wrapper(hass, entry, data)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="migration_reauth_required",
            translation_placeholders={"version": str(entry.version)},
        )
    if entry.version == 2:
        await _reauth_flow_wrapper(hass, entry, data)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="migration_reauth_required",
            translation_placeholders={"version": str(entry.version)},
        )
    version = entry.version
    if version == 3:
        # Migrate device_id to hardware_id for blinkpy 0.25.x OAuth2 compatibility
        if "device_id" in data:
            data["hardware_id"] = data.pop("device_id")
        version = 4
    if version == 4:
        # Blink's OAuth endpoint rejects LEGACY_HARDWARE_ID; drop it so blinkpy
        # mints a fresh per-install UUID on next setup.
        if data.get("hardware_id") == LEGACY_HARDWARE_ID:
            data.pop("hardware_id", None)
        version = 5

    if version != entry.version or data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=data, version=version)
    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Blink."""

    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: BlinkConfigEntry) -> bool:
    """Set up Blink via config entry."""
    _async_import_options_from_data_if_missing(hass, entry)
    session = async_blink_session(hass)
    blink = Blink(session=session)
    auth_data = deepcopy(dict(entry.data))
    blink.auth = BlinkPlusAuth(
        auth_data,
        no_prompt=True,
        session=session,
        callback=lambda: _async_update_entry_data(hass, entry, blink),
    )
    blink.refresh_rate = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    blink.blink_plus_light_local_storage = True
    blink.blink_plus_usb_poll_minutes = int(
        entry.options.get(CONF_USB_MOTION_MINUTES, DEFAULT_USB_MOTION_MINUTES)
    )
    _patch_local_storage_uploads()
    coordinator = BlinkUpdateCoordinator(hass, entry, blink)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await _async_start_live_servers(entry, coordinator)
    try:
        coordinator.options_snapshot = dict(entry.options)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

        await _async_setup_archive(hass, entry, coordinator)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # A failed setup is not unloaded: free the listening ports ourselves,
        # or the retry can't bind them again.
        await _async_stop_live_servers(coordinator)
        if coordinator.archiver is not None:
            coordinator.archiver.shutdown()
        raise

    # Run the first full (possibly slow) Blink refresh after setup has finished,
    # in the background, instead of inside setup.
    async def _first_refresh(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_call_later(hass, 20, _first_refresh))
    return True


async def _async_setup_archive(
    hass: HomeAssistant, entry: BlinkConfigEntry, coordinator: BlinkUpdateCoordinator
) -> None:
    """Copy USB clips to a folder on a schedule (if a folder is configured)."""
    folder = (entry.options.get(CONF_ARCHIVE_FOLDER) or DEFAULT_ARCHIVE_FOLDER).strip()
    if not folder:
        return
    archiver = ClipArchiver(hass, coordinator, entry.entry_id, folder)
    await archiver.async_load()
    coordinator.archiver = archiver
    # Unload: no new runs and no pending resume. A run still in progress
    # finishes its current clip and then stops scheduling itself.
    entry.async_on_unload(archiver.shutdown)
    for server in coordinator.live_servers.values():
        server.sync_busy = lambda: archiver.usb_busy
    if not hass.config.is_allowed_path(folder):
        archiver.status["last_error"] = (
            f"{folder} is not an allowed folder. Use /media/... or add it to "
            "allowlist_external_dirs"
        )
        _LOGGER.error("Blink clip archive: %s", archiver.status["last_error"])
        archiver.enabled = False
        return
    hours = float(entry.options.get(CONF_ARCHIVE_HOURS, DEFAULT_ARCHIVE_HOURS))
    minutes = int(entry.options.get(CONF_ARCHIVE_INTERVAL, DEFAULT_ARCHIVE_INTERVAL))
    if minutes <= 0:
        return

    async def _scheduled(_now) -> None:
        await archiver.async_run(hours=hours)

    entry.async_on_unload(
        async_track_time_interval(hass, _scheduled, timedelta(minutes=minutes))
    )
    # First run a few minutes after start-up, not during it.
    entry.async_on_unload(async_call_later(hass, 180, _scheduled))


async def _async_start_live_servers(
    entry: BlinkConfigEntry, coordinator: BlinkUpdateCoordinator
) -> None:
    """Start one local live view server per camera."""
    opts = entry.options
    host = opts.get(CONF_STREAM_HOST, DEFAULT_STREAM_HOST)
    base_port = int(opts.get(CONF_STREAM_BASE_PORT, DEFAULT_STREAM_BASE_PORT))
    # Stable port order: sort by serial so a camera keeps its port across restarts.
    cameras = sorted(
        coordinator.api.cameras.values(), key=lambda cam: str(cam.serial or live_key(cam))
    )
    for index, camera in enumerate(cameras):
        server = BlinkLiveViewServer(
            coordinator.api,
            camera,
            host=host,
            port=base_port + index if base_port else 0,
            linger=float(opts.get(CONF_STREAM_LINGER, DEFAULT_STREAM_LINGER)),
            max_minutes=float(
                opts.get(CONF_STREAM_MAX_MINUTES, DEFAULT_STREAM_MAX_MINUTES)
            ),
            token_mode=opts.get(CONF_STREAM_TOKEN_MODE, DEFAULT_STREAM_TOKEN_MODE),
        )
        try:
            await server.async_start()
        except OSError as err:
            _LOGGER.error(
                "Could not start live view server for %s on %s:%s: %s",
                camera.name,
                host,
                server.port,
                err,
            )
            continue
        coordinator.live_servers[live_key(camera)] = server
        _LOGGER.info("Blink live view for %s at %s", camera.name, server.url)


async def _async_options_updated(hass: HomeAssistant, entry: BlinkConfigEntry) -> None:
    """Reload when options change (token refreshes also land here, ignore them)."""
    if dict(entry.options) == getattr(entry.runtime_data, "options_snapshot", None):
        return
    await hass.config_entries.async_reload(entry.entry_id)


_PATCHED = False


def _patch_local_storage_uploads() -> None:
    """Stop blinkpy from uploading every new USB clip to the cloud on each poll.

    blinkpy calls LocalStorageMediaItem.prepare_download() for each new clip in
    the Sync Module 2 manifest, which makes the sync module upload it, one at a
    time. That keeps the module "busy" (the app can't live view or play clips)
    and isn't needed for motion detection, which only reads the manifest.
    Only affects Blink Plus's own Blink instance, not the built-in integration.
    """
    global _PATCHED  # noqa: PLW0603
    if _PATCHED:
        return
    try:
        from blinkpy.sync_module import LocalStorageMediaItem  # noqa: PLC0415
    except ImportError:
        return
    original = LocalStorageMediaItem.prepare_download

    async def prepare_download(self, blink, *args, **kwargs):
        if getattr(blink, "blink_plus_light_local_storage", False):
            return None
        return await original(self, blink, *args, **kwargs)

    LocalStorageMediaItem.prepare_download = prepare_download

    # Reading the USB clip list makes the sync module scan the drive, and while
    # it does that the Blink app gets "system busy" when browsing saved clips.
    from blinkpy.sync_module import BlinkSyncModule  # noqa: PLC0415

    original_manifest = BlinkSyncModule.update_local_storage_manifest

    async def update_local_storage_manifest(self, *args, **kwargs):
        minutes = getattr(self.blink, "blink_plus_usb_poll_minutes", None)
        if minutes is None:  # not ours (built-in Blink integration)
            return await original_manifest(self, *args, **kwargs)
        now = time.monotonic()
        last = getattr(self, "_blink_plus_last_manifest", None)
        if minutes <= 0 or (last is not None and now - last < minutes * 60):
            self._local_storage["manifest_stale"] = True
            return None
        self._blink_plus_last_manifest = now
        return await original_manifest(self, *args, **kwargs)

    BlinkSyncModule.update_local_storage_manifest = update_local_storage_manifest

    # blinkpy sends POST /network/{id}/update (a sync module *command*) on every
    # poll and waits for it. The same armed/online state is already in the
    # homescreen we fetch anyway, so read it from there.
    original_network_info = BlinkSyncModule.get_network_info

    async def get_network_info(self, *args, **kwargs):
        if getattr(self.blink, "blink_plus_usb_poll_minutes", None) is None:
            return await original_network_info(self, *args, **kwargs)
        for net in (self.blink.homescreen or {}).get("networks", []) or []:
            if str(net.get("id")) == str(self.network_id):
                self.network_info = {
                    "network": {
                        "id": net.get("id"),
                        "name": net.get("name"),
                        "armed": net.get("armed"),
                        "sync_module_error": False,
                    }
                }
                return True
        return await original_network_info(self, *args, **kwargs)

    BlinkSyncModule.get_network_info = get_network_info

    # blinkpy also asks every camera for live signal readings on each poll
    # (GET /network/{n}/camera/{id}/signals). Temperature/battery/wifi are in
    # the homescreen already.
    from blinkpy.camera import BlinkCamera as _BlinkCamera  # noqa: PLC0415

    original_sensor_info = _BlinkCamera.get_sensor_info

    async def get_sensor_info(self, *args, **kwargs):
        if getattr(self.sync.blink, "blink_plus_usb_poll_minutes", None) is None:
            return await original_sensor_info(self, *args, **kwargs)
        self.temperature_calibrated = self.temperature
        return None

    _BlinkCamera.get_sensor_info = get_sensor_info
    _PATCHED = True


@callback
def _async_update_entry_data(
    hass: HomeAssistant, entry: BlinkConfigEntry, blink: Blink
) -> None:
    """Update the config entry data after token refresh."""
    hass.config_entries.async_update_entry(entry, data=blink.auth.login_attributes)


@callback
def _async_import_options_from_data_if_missing(
    hass: HomeAssistant, entry: BlinkConfigEntry
) -> None:
    options = dict(entry.options)
    if CONF_SCAN_INTERVAL not in entry.options:
        options[CONF_SCAN_INTERVAL] = entry.data.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        hass.config_entries.async_update_entry(entry, options=options)


async def async_unload_entry(hass: HomeAssistant, entry: BlinkConfigEntry) -> bool:
    """Unload Blink entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await _async_stop_live_servers(entry.runtime_data)
    return unloaded


async def _async_stop_live_servers(coordinator: BlinkUpdateCoordinator) -> None:
    """Stop every local live view server (ends running Blink sessions)."""
    for server in list(coordinator.live_servers.values()):
        try:
            await server.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Stopping live view server failed", exc_info=True)
    coordinator.live_servers.clear()
