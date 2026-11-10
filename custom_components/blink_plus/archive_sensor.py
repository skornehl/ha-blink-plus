"""Clip archive status sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util import dt as dt_util

from .button import account_device


class ClipArchiveSensor(SensorEntity):
    """Time of the newest clip copied to the archive folder."""

    _attr_has_entity_name = True
    _attr_translation_key = "clip_archive"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    def __init__(self, entry, archiver) -> None:
        """Initialize."""
        self._archiver = archiver
        self._attr_unique_id = f"{entry.entry_id}-clip-archive"
        self._attr_device_info = account_device(entry)

    async def async_added_to_hass(self) -> None:
        """Update when the archiver reports progress."""
        self._archiver.listeners.append(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self._archiver.listeners.remove(self.async_write_ha_state)
        )

    @property
    def native_value(self):
        """Creation time of the newest archived clip."""
        last = self._archiver.status.get("last_clip")
        return dt_util.parse_datetime(last) if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Archive details."""
        st = self._archiver.status
        return {
            "folder": self._archiver.folder,
            "status": st["state"],
            "last_run": st["last_run"],
            "last_result": st["last_result"],
            "last_clip_camera": st["last_clip_camera"],
            "last_clip_path": st["last_clip_path"],
            "saved_last_run": st["saved_last_run"],
            "saved_total": st["saved_total"],
            "last_error": st["last_error"],
            "next_auto_resume_s": st.get("next_auto_resume_s"),
            "skipped_clips": st.get("skipped_clips", 0),
        }
