"""Services for the Blink integration."""

import voluptuous as vol

from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.const import CONF_FILE_PATH, CONF_FILENAME
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import config_validation as cv, service

from .const import DOMAIN

SERVICE_RECORD = "record"
SERVICE_TRIGGER = "trigger_camera"
SERVICE_SAVE_VIDEO = "save_video"
SERVICE_SAVE_RECENT_CLIPS = "save_recent_clips"
SERVICE_STOP_LIVE_VIEW = "stop_live_view"
SERVICE_ARCHIVE_CLIPS = "archive_clips"
SERVICE_RESET_ARCHIVE = "reset_archive"
SERVICE_PLAY_AUDIO = "play_audio"


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the Blink integration."""

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RECORD,
        entity_domain=CAMERA_DOMAIN,
        schema=None,
        func="record",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_TRIGGER,
        entity_domain=CAMERA_DOMAIN,
        schema=None,
        func="trigger_camera",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SAVE_RECENT_CLIPS,
        entity_domain=CAMERA_DOMAIN,
        schema={vol.Required(CONF_FILE_PATH): cv.string},
        func="save_recent_clips",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SAVE_VIDEO,
        entity_domain=CAMERA_DOMAIN,
        schema={vol.Required(CONF_FILENAME): cv.string},
        func="save_video",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_STOP_LIVE_VIEW,
        entity_domain=CAMERA_DOMAIN,
        schema=None,
        func="stop_live_view",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_PLAY_AUDIO,
        entity_domain=CAMERA_DOMAIN,
        schema={vol.Required(CONF_FILE_PATH): cv.string},
        func="play_audio",
    )

    async def _archive_clips(call: ServiceCall) -> ServiceResponse:
        """Copy new USB clips to the archive folder of every Blink Plus account."""
        results = {}
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            archiver = getattr(entry.runtime_data, "archiver", None)
            if archiver is None:
                results[entry.title] = {"result": "archive folder not set (Options)"}
                continue
            results[entry.title] = await archiver.async_run(
                hours=float(call.data.get("hours", 24)),
                max_clips=int(call.data.get("max_clips", 25)),
            )
        return results

    hass.services.async_register(
        DOMAIN,
        SERVICE_ARCHIVE_CLIPS,
        _archive_clips,
        schema=vol.Schema(
            {
                vol.Optional("hours", default=24): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=24 * 365)
                ),
                vol.Optional("max_clips", default=25): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=500)
                ),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _reset_archive(call: ServiceCall) -> None:
        """Forget which clips were copied, so the next run copies them again."""
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            archiver = getattr(entry.runtime_data, "archiver", None)
            if archiver is not None:
                await archiver.async_reset()

    hass.services.async_register(DOMAIN, SERVICE_RESET_ARCHIVE, _reset_archive)
