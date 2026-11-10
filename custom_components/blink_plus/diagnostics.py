"""Diagnostics support for Blink."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .blinkpy_patches import apply_blinkpy_patches
from .coordinator import BlinkConfigEntry

TO_REDACT = {
    "serial",
    "macaddress",
    "username",
    "password",
    "token",
    "unique_id",
    "hardware_id",
    "access_token",
    "refresh_token",
    "liveview_token",
    "live_view_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    config_entry: BlinkConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    api = config_entry.runtime_data.api

    data = {
        camera.name: dict(camera.attributes.items())
        for _, camera in api.cameras.items()
    }

    live = {
        camera_name: server.stats.as_dict()
        for camera_name, server in (
            (srv.name, srv) for srv in config_entry.runtime_data.live_servers.values()
        )
    }

    return {
        "config_entry": async_redact_data(config_entry.as_dict(), TO_REDACT),
        "cameras": async_redact_data(data, TO_REDACT),
        "live_view": live,
        "blinkpy_load_reductions": apply_blinkpy_patches(),
        "blink_api_activity": list(getattr(api.auth, "activity", []))[-150:],
    }
