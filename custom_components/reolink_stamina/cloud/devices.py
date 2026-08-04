"""The devices a syncer sits between: the recorder it reads and the account it writes to.

A syncer serves exactly one NVR, identified by the Reolink config entry it belongs to. These
read the device registry rather than the live Reolink API, so a recorder that is offline or
not loaded yet still resolves to a name and a device — which is what lets a syncer start up
and wait for it rather than vanish until the next Home Assistant restart.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from ..const import REOLINK_DOMAIN


@callback
def async_nvr_device(hass: HomeAssistant, entry_id: str) -> dr.DeviceEntry | None:
    """Return the recorder device belonging to a Reolink config entry.

    The recorder itself, not its cameras: in the Reolink integration a camera is a device
    hanging off the NVR by `via_device`, so the root device is the NVR.
    """
    devices = dr.async_get(hass)
    for device in devices.devices.values():
        if device.via_device_id is None and entry_id in device.config_entries:
            return device
    return None


@callback
def async_nvr_identifier(hass: HomeAssistant, entry_id: str) -> tuple[str, str] | None:
    """Return the recorder's identifier, for linking the sync device to it.

    `via_device` needs an identifier rather than a device id, and it is what makes Home
    Assistant show the relationship on both pages: the sync device appears under the NVR's
    connected devices, and names its recorder in return.
    """
    device = async_nvr_device(hass, entry_id)
    if device is None:
        return None
    return next((item for item in device.identifiers if item[0] == REOLINK_DOMAIN), None)


@callback
def async_nvr_name(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return what the user calls this recorder, preferring their own name for it."""
    device = async_nvr_device(hass, entry_id)
    if device is None:
        return None
    return device.name_by_user or device.name


@callback
def async_entry_device_name(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return the device name a config entry created, if it made one.

    Home Assistant allows a device exactly one parent, so a syncer descends from its recorder
    — the thing whose absence explains missing clips. The cloud account it writes to is a
    device too, and this is how it gets named alongside rather than lost.
    """
    devices = dr.async_get(hass)
    for device in devices.devices.values():
        if entry_id in device.config_entries:
            return device.name_by_user or device.name
    return None
