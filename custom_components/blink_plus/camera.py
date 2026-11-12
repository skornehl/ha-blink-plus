"""Support for Blink system camera."""

from collections.abc import Mapping
import logging
from typing import Any, override

from blinkpy.auth import UnauthorizedError
from blinkpy.camera import BlinkCamera as BlinkCameraAPI
from requests.exceptions import ChunkedEncodingError

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_BRAND, DOMAIN
from .coordinator import BlinkConfigEntry, BlinkUpdateCoordinator
from .immis import BlinkLiveViewServer, live_key
from .live_hold import async_stop_hold

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BlinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a Blink Camera."""

    coordinator = config_entry.runtime_data
    entities = [
        BlinkCamera(coordinator, name, camera)
        for name, camera in coordinator.api.cameras.items()
    ]

    async_add_entities(entities)


class BlinkCamera(CoordinatorEntity[BlinkUpdateCoordinator], Camera):
    """An implementation of a Blink Camera."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, coordinator: BlinkUpdateCoordinator, name, camera: BlinkCameraAPI
    ) -> None:
        """Initialize a camera."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self._camera = camera
        # Legacy format, kept as migrating existing unique IDs is not worth the risk
        self._attr_unique_id = f"{camera.serial}-camera"  # pylint: disable=home-assistant-entity-unique-id-redundant-platform
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, camera.serial)},
            serial_number=camera.serial,
            sw_version=camera.version,
            name=name,
            manufacturer=DEFAULT_BRAND,
            model=camera.camera_type,
        )
        self._live: BlinkLiveViewServer | None = coordinator.live_servers.get(
            live_key(camera)
        )
        if self._live is not None:
            self._attr_supported_features = CameraEntityFeature.STREAM
        _LOGGER.debug("Initialized blink camera %s", self._camera.name)

    @override
    async def async_added_to_hass(self) -> None:
        """Hook live view state changes to the entity state."""
        await super().async_added_to_hass()
        if self._live is not None:
            self._live.on_change = self._live_changed
            self._live.on_preempt = self._live_preempted
            self._live.entity_id = self.entity_id

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Unhook live view callbacks."""
        if self._live is not None:
            self._live.on_change = None
            self._live.on_preempt = None
            self._live.entity_id = None
        await super().async_will_remove_from_hass()

    def _live_preempted(self) -> None:
        """Another camera on this sync module took over: stop HA's stream too."""
        if self.stream is not None and self.hass is not None:
            self.hass.async_create_task(self.stream.stop())

    def _live_changed(self) -> None:
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    @override
    async def stream_source(self) -> str | None:
        """Return the local MPEG-TS feed. Opening it starts a Blink live view."""
        return self._live.url if self._live else None

    @property
    @override
    def is_streaming(self) -> bool:
        """Return True while a Blink live view session is running."""
        return bool(self._live and self._live.stats.state == "streaming")

    async def stop_live_view(self) -> None:
        """End the live view session now (saves battery)."""
        # Stop whatever keeps it open first (start_live_view's hold, HA's own
        # stream worker), or it would reconnect at once and start a new Blink
        # session.
        if self._live:
            await async_stop_hold(self._live)
        if self.stream is not None:
            await self.stream.stop()
        if self._live:
            await self._live.async_stop_session()

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the camera attributes."""
        attrs = dict(self._camera.attributes or {})
        if self._live is not None:
            attrs.update(self._live.stats.as_dict())
            attrs["live_view_url"] = self._live.url
        return attrs

    @override
    async def async_enable_motion_detection(self) -> None:
        """Enable motion detection for the camera."""
        try:
            await self._camera.async_arm(True)
        except TimeoutError as er:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_arm",
            ) from er
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er

        self._camera.motion_enabled = True
        await self.coordinator.async_refresh()

    @override
    async def async_disable_motion_detection(self) -> None:
        """Disable motion detection for the camera."""
        try:
            await self._camera.async_arm(False)
        except TimeoutError as er:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_disarm",
            ) from er
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er

        self._camera.motion_enabled = False
        await self.coordinator.async_refresh()

    @property
    @override
    def motion_detection_enabled(self) -> bool:
        """Return the state of the camera."""
        return self._camera.arm

    @property
    @override
    def brand(self) -> str | None:
        """Return the camera brand."""
        return DEFAULT_BRAND

    async def record(self) -> None:
        """Trigger camera to record a clip."""
        try:
            await self._camera.record()
        except TimeoutError as er:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_clip",
            ) from er
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er

        self.async_write_ha_state()

    async def trigger_camera(self) -> None:
        """Trigger camera to take a snapshot."""
        try:
            await self._camera.snap_picture()
        except TimeoutError as er:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_snap",
            ) from er
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er

        self.async_write_ha_state()

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a frame from the live view if one is running, else the thumbnail.

        We never start a live view just for a still: that would wake the camera
        every time Apple Home or a dashboard refreshes. Nor does a still keep a
        running live view alive: grabs use ``still_url`` and don't count as
        viewers, so the session ends when the last real viewer leaves.
        """
        if self._live is not None and self._live.stats.state == "streaming":
            try:
                from homeassistant.components.ffmpeg import async_get_image  # noqa: PLC0415

                # still_url: this grab must not keep the live view running.
                image = await async_get_image(
                    self.hass, self._live.still_url, width=width, height=height
                )
                if image:
                    return image
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Live frame grab failed for %s", self.name, exc_info=True)
        return await self.hass.async_add_executor_job(self.camera_image, width, height)

    @override
    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        try:
            return self._camera.image_from_cache
        except ChunkedEncodingError:
            _LOGGER.debug("Could not retrieve image for %s", self._camera.name)
            return None
        except TypeError:
            _LOGGER.debug("No cached image for %s", self._camera.name)
            return None

    async def save_recent_clips(self, file_path) -> None:
        """Save multiple recent clips to output directory."""
        if not self.hass.config.is_allowed_path(file_path):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_path",
                translation_placeholders={"target": file_path},
            )

        try:
            await self._camera.save_recent_clips(output_dir=file_path)
        except OSError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="cant_write",
            ) from err
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er

    async def save_video(self, filename) -> None:
        """Handle save video service calls."""
        if not self.hass.config.is_allowed_path(filename):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_path",
                translation_placeholders={"target": filename},
            )

        try:
            await self._camera.video_to_file(filename)
        except OSError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="cant_write",
            ) from err
        except UnauthorizedError as er:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Blink authorization failed") from er
