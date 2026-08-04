"""The switch that decides whether new clips are accepted for upload.

Deliberately not a gate on the upload itself: turning it off stops *new* events being taken
on, and anything already queued still goes. Disarming an alarm should not throw away the
footage of whatever made you disarm it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN


def _device_info(syncer) -> DeviceInfo:
    """Describe the sync device, linked to the recorder it reads from.

    One sync device per recorder, so it is the counterpart of the NVR device the Reolink
    integration creates. `via_device` is what makes Home Assistant show the relationship on
    both pages — the sync device appears under the NVR's connected devices, and names its
    recorder in return.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, syncer.subentry.subentry_id)},
        name=f"Cloud sync {syncer.nvr_name}",
        manufacturer="Reolink Stamina",
        model=syncer.destination.label,
    )
    if syncer.nvr_device is not None:
        info["via_device"] = syncer.nvr_device
    return info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one switch per configured recorder."""
    data = hass.data.get(DOMAIN)
    if data is None:
        return
    for subentry_id, syncer in data.syncers.items():
        async_add_entities([CloudSyncSwitch(syncer)], config_subentry_id=subentry_id)


class CloudSyncSwitch(SwitchEntity):
    """Accept new clips for this recorder, or do not."""

    _attr_has_entity_name = True
    _attr_name = "Cloud sync"
    _attr_icon = "mdi:cloud-upload-outline"
    _attr_should_poll = False

    def __init__(self, syncer) -> None:
        """Bind to one recorder's syncer."""
        self._syncer = syncer
        self._attr_unique_id = f"{syncer.subentry.subentry_id}_accepting"
        self._attr_device_info = _device_info(syncer)

    async def async_added_to_hass(self) -> None:
        """Follow the syncer's state."""
        self.async_on_remove(self._syncer.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        """Whether new clips are being accepted."""
        return self._syncer.accepting

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain what "off" means, since it is not what people assume."""
        return {
            "queued": self._syncer.status.queued,
            "events_in_progress": self._syncer.status.pending_windows,
            "recorder": self._syncer.nvr_name,
            "cameras": self._syncer.camera_names,
            "destination": self._syncer.destination.label,
            "cloud_account": self._syncer.destination_device,
            "note": "Off stops new clips being accepted; queued clips still upload",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start accepting new clips."""
        await self._syncer.async_set_accepting(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop accepting new clips."""
        await self._syncer.async_set_accepting(False)
