"""Blink Coordinator."""

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any, override

from aiohttp import ClientError
from blinkpy.auth import BlinkTwoFARequiredError, UnauthorizedError
from blinkpy.blinkpy import Blink

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = 300
# HA cancels an integration whose setup takes over 300 s. blinkpy's refresh can
# take minutes on Sync Module 2 local storage (it asks the sync module to upload
# every new USB clip one by one), so it must never run during setup and must
# never run unbounded.
REFRESH_TIMEOUT = 240

type BlinkConfigEntry = ConfigEntry[BlinkUpdateCoordinator]


class BlinkUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """BlinkUpdateCoordinator - In charge of downloading the data for a site."""

    config_entry: BlinkConfigEntry

    def __init__(
        self, hass: HomeAssistant, config_entry: BlinkConfigEntry, api: Blink
    ) -> None:
        """Initialize the data service."""
        self.api = api
        # serial -> BlinkLiveViewServer, filled in by __init__.async_setup_entry
        self.live_servers: dict[str, Any] = {}
        self._skip_next_refresh = True
        self.archiver: Any = None  # ClipArchiver when an archive folder is set
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

    @override
    async def _async_setup(self):
        """Set up the coordinator."""
        try:
            await self.api.start()
        except (ClientError, TimeoutError) as ex:
            raise ConfigEntryNotReady("Can not connect to host") from ex
        except (BlinkTwoFARequiredError, UnauthorizedError) as ex:
            raise ConfigEntryAuthFailed("Required Blink re-authentication") from ex
        except Exception as ex:
            raise ConfigEntryError("Unknown error connecting to Blink") from ex

        if not self.api.available:
            raise ConfigEntryNotReady

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Async update wrapper."""
        if self._skip_next_refresh:
            # Setup: api.start() has already loaded cameras, thumbnails and states.
            # The first real refresh is scheduled from __init__ right after setup.
            self._skip_next_refresh = False
            return {}
        if any(srv.active for srv in self.live_servers.values()):
            # Never poll the sync module while it is busy with a live view.
            _LOGGER.debug("Live view running, skipping this Blink refresh")
            return self.data or {}
        started = time.monotonic()
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT):
                result = await self.api.refresh()
        except TimeoutError as ex:
            raise UpdateFailed(
                f"Blink refresh took longer than {REFRESH_TIMEOUT}s "
                "(sync module local storage busy?), will retry next poll"
            ) from ex
        except UnauthorizedError as ex:
            raise ConfigEntryAuthFailed("Blink API authorization failed") from ex
        except ClientError as ex:
            # Network/HTTP hiccup: a normal failed poll, not an "unexpected error"
            # with a traceback.
            raise UpdateFailed(f"Blink refresh failed: {ex}") from ex
        took = time.monotonic() - started
        if took > 60:
            _LOGGER.warning("Blink refresh took %.0f s", took)
        return result
