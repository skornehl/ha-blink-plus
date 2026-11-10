"""Load reductions for Blink Plus's own blinkpy instance.

blinkpy does a few things on every poll that keep the Sync Module busy (the
Blink app then says "system busy" and live view fails). Blink Plus replaces
four blinkpy methods with versions that skip that work, but only for its own
Blink instance (marked with ``blink_plus_usb_poll_minutes`` /
``blink_plus_light_local_storage``). The built-in Blink integration shares the
same blinkpy classes and keeps the original behaviour.

blinkpy ships with Home Assistant and its internals can change with any HA
update. Every patch is therefore optional: if a class or method is gone, or a
patched call meets an internal it doesn't know, Blink Plus logs it once and
falls back to blinkpy's original behaviour (a bit more load, nothing breaks).
"""

from __future__ import annotations

from collections.abc import Callable
import importlib
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_APPLIED: dict[str, bool] | None = None


def _ours(blink: Any) -> bool:
    """True for Blink Plus's own Blink instance."""
    return getattr(blink, "blink_plus_usb_poll_minutes", None) is not None


def _wrap(module: str, cls_name: str, method: str, make: Callable[[Any], Any]) -> bool:
    """Replace cls.method with make(original). False (and a warning) if impossible."""
    try:
        cls = getattr(importlib.import_module(module), cls_name)
        original = getattr(cls, method)
    except (ImportError, AttributeError):
        _LOGGER.warning(
            "blinkpy has no %s.%s.%s (changed in a Home Assistant update?). "
            "Blink Plus keeps working without this load reduction",
            module, cls_name, method,
        )
        return False
    if not callable(original):
        _LOGGER.warning("blinkpy %s.%s is not callable, not patched", cls_name, method)
        return False
    setattr(cls, method, make(original))
    return True


def _patch_prepare_download(original):
    """Don't make the sync module upload every new USB clip on each poll."""

    async def prepare_download(self, blink, *args, **kwargs):
        if getattr(blink, "blink_plus_light_local_storage", False):
            return None
        return await original(self, blink, *args, **kwargs)

    return prepare_download


def _patch_manifest(original):
    """Read the USB clip list at most every N minutes (it makes the module scan the drive)."""

    async def update_local_storage_manifest(self, *args, **kwargs):
        blink = getattr(self, "blink", None)
        if not _ours(blink):
            return await original(self, *args, **kwargs)
        minutes = blink.blink_plus_usb_poll_minutes
        now = time.monotonic()
        last = getattr(self, "_blink_plus_last_manifest", None)
        if minutes <= 0 or (last is not None and now - last < minutes * 60):
            try:
                self._local_storage["manifest_stale"] = True
            except (AttributeError, KeyError, TypeError):
                pass  # blinkpy changed its internals: just skip the flag
            return None
        self._blink_plus_last_manifest = now
        return await original(self, *args, **kwargs)

    return update_local_storage_manifest


def _patch_network_info(original):
    """Take armed/online from the homescreen instead of a sync module command."""

    async def get_network_info(self, *args, **kwargs):
        blink = getattr(self, "blink", None)
        if not _ours(blink):
            return await original(self, *args, **kwargs)
        networks = (getattr(blink, "homescreen", None) or {}).get("networks") or []
        for net in networks:
            if str(net.get("id")) == str(getattr(self, "network_id", None)):
                self.network_info = {
                    "network": {
                        "id": net.get("id"),
                        "name": net.get("name"),
                        "armed": net.get("armed"),
                        "sync_module_error": False,
                    }
                }
                return True
        return await original(self, *args, **kwargs)

    return get_network_info


def _patch_sensor_info(original):
    """Skip the per-camera signals request; the homescreen has the same values."""

    async def get_sensor_info(self, *args, **kwargs):
        blink = getattr(getattr(self, "sync", None), "blink", None)
        if not _ours(blink):
            return await original(self, *args, **kwargs)
        self.temperature_calibrated = getattr(self, "temperature", None)
        return None

    return get_sensor_info


def apply_blinkpy_patches() -> dict[str, bool]:
    """Apply all load reductions once. Returns which ones are active."""
    global _APPLIED  # noqa: PLW0603
    if _APPLIED is not None:
        return _APPLIED
    _APPLIED = {
        "prepare_download": _wrap(
            "blinkpy.sync_module", "LocalStorageMediaItem", "prepare_download",
            _patch_prepare_download,
        ),
        "local_storage_manifest": _wrap(
            "blinkpy.sync_module", "BlinkSyncModule", "update_local_storage_manifest",
            _patch_manifest,
        ),
        "network_info": _wrap(
            "blinkpy.sync_module", "BlinkSyncModule", "get_network_info",
            _patch_network_info,
        ),
        "sensor_info": _wrap(
            "blinkpy.camera", "BlinkCamera", "get_sensor_info", _patch_sensor_info,
        ),
    }
    return _APPLIED
