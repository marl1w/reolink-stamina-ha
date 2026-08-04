"""What each recorder's syncer is doing.

Enough to automate on: how much of the quota is left, how much is waiting, whether the last
upload worked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
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


@dataclass(frozen=True, kw_only=True)
class SyncSensor(SensorEntityDescription):
    """A reading taken from a syncer's status."""

    value: Callable[[object], object]


SENSORS: tuple[SyncSensor, ...] = (
    SyncSensor(
        key="quota_used",
        translation_key="quota_used",
        name="Quota used",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        value=lambda status: status.used,
    ),
    SyncSensor(
        key="quota_free",
        translation_key="quota_free",
        name="Quota available",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        value=lambda status: status.free,
    ),
    SyncSensor(
        key="clips",
        translation_key="clips",
        name="Clips stored",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda status: status.clips,
    ),
    SyncSensor(
        key="queued",
        translation_key="queued",
        name="Queued uploads",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda status: status.queued,
    ),
    SyncSensor(
        key="uploaded",
        translation_key="uploaded",
        name="Uploads since restart",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda status: status.uploaded,
    ),
    SyncSensor(
        key="last_upload",
        translation_key="last_upload",
        name="Last upload",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda status: status.last_upload,
    ),
    SyncSensor(
        key="last_error",
        translation_key="last_error",
        name="Last error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: (status.last_error or "none")[:255],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the status sensors for every configured recorder."""
    data = hass.data.get(DOMAIN)
    if data is None:
        return
    for subentry_id, syncer in data.syncers.items():
        async_add_entities(
            [CloudSyncSensor(syncer, description) for description in SENSORS],
            config_subentry_id=subentry_id,
        )


class CloudSyncSensor(SensorEntity):
    """One reading from one recorder's syncer."""

    entity_description: SyncSensor
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, syncer, description: SyncSensor) -> None:
        """Bind to a syncer and a reading."""
        self._syncer = syncer
        self.entity_description = description
        self._attr_unique_id = f"{syncer.subentry.subentry_id}_{description.key}"
        self._attr_device_info = _device_info(syncer)

    async def async_added_to_hass(self) -> None:
        """Follow the syncer's state."""
        self.async_on_remove(self._syncer.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> object:
        """Return the current reading."""
        value = self.entity_description.value(self._syncer.status)
        if isinstance(value, dt.datetime) and value.tzinfo is None:
            return value.astimezone()
        return value
