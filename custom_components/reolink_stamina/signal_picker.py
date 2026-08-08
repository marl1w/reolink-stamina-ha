"""Which entities the signal picker offers, and which it quietly leaves out.

Policy, not flow. It is the longest-lived thing in the options flow and the thing most likely
to change — every filter here was added because a real installation showed it was needed, and
the numbers in the comments are from that installation. Keeping it beside three unrelated
config flows meant a question about what the picker offers began by scrolling past cloud sync.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    RELEVANCE_SIGNAL_DOMAINS,
    RELEVANCE_SIGNAL_ENUM_DOMAINS,
)
from .relevance.watcher import async_detection_map, async_signal_map

# Device classes that describe a device's own health rather than anything happening in the
# house. Smoke, gas and carbon monoxide are deliberately absent: they are rare by definition,
# which is exactly what this feature is for.
_SELF_REPORTING = frozenset(
    {
        "battery",
        "battery_charging",
        "connectivity",
        "problem",
        "running",
        "tamper",
        "update",
    }
)


def async_unhelpful_signals(hass: HomeAssistant) -> list[str]:
    """Entities the signal picker should not offer.

    The picker opens onto every binary sensor in the house, and most of them are noise in the
    literal sense: a Reolink NVR alone contributes a dozen per camera. Two families are worth
    hiding rather than leaving somebody to scroll past.

    **The cameras' own detection sensors.** These *are* the detections. Counting `person` on
    the drive as a signal against an event that is a person on the drive teaches the model that
    a person is usually accompanied by a person, and every event scores as ordinary. It is not
    a subtle mistake, but nothing in the picker warns of it.

    Only the detection sensors, though — the rest of what Reolink publishes is some of the best
    material there is. A floodlight that came on, a siren that fired, and above all the day/night
    state, which is the camera saying whether it switched to infrared: darkness as this lens
    actually experienced it, rather than as an almanac calculated it for the whole property.

    **An alarm panel's own children.** An alarm exposes its arming state and, beside it, a
    sensor per zone, per fault and per tamper. The state is the useful signal; the zones are
    the same door contacts a camera is already pointed at, and picking twenty of them makes
    every event carry twenty terms saying "the hall was quiet".

    **Equipment talking about itself.** Home Assistant already marks these `diagnostic`: an
    add-on's "running", a printer's firmware check, a satellite dish's "motors stuck". On the
    installation this was measured against they were a quarter of everything on offer.

    **Entities Home Assistant has disabled.** They have no state and no history, so picking one
    contributes a term that is permanently unknown. Entities merely *hidden* stay: hiding is a
    decision about dashboards, and on that same installation it is the alarm panels that are
    hidden — the single most useful signal in the house.

    Anything that cannot be resolved is left in the list. A picker that hides something the
    user wanted is worse than one that offers something they will not pick.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    # This integration's own entities, and the detection sensors the model already counts.
    # Deliberately not every Reolink entity: see above.
    ours = {
        entry.entry_id for entry in hass.config_entries.async_entries() if entry.domain == DOMAIN
    }
    detections = set(async_detection_map(hass, include_all_devices=True))
    # And whatever each camera already contributes about itself. Those are attached to their
    # own camera automatically, so offering them here would only let somebody attach one
    # camera's floodlight to all of them.
    automatic = {
        entity_id
        for entities in async_signal_map(hass, {}, include_all_devices=True).values()
        for entity_id in entities
    }

    # Devices carrying an alarm panel. Their other entities are that panel's parts.
    alarms = {
        entry.device_id
        for entry in entities.entities.values()
        if entry.domain == "alarm_control_panel" and entry.device_id is not None
    }
    # A wired system usually models each zone as its own device hanging off the panel, so the
    # children are found through `via_device` rather than by sharing one. Deliberately not by
    # area: an alarm's area is the house, and that would hide everything in it.
    alarm_children = {
        device.id
        for device in devices.devices.values()
        if device.via_device_id is not None and device.via_device_id in alarms
    }

    parts = alarms | alarm_children
    hidden: list[str] = []
    for entry in entities.entities.values():
        if entry.domain not in (*RELEVANCE_SIGNAL_DOMAINS, *RELEVANCE_SIGNAL_ENUM_DOMAINS):
            continue
        if entry.disabled_by is not None:
            hidden.append(entry.entity_id)
            continue
        # The panel's own state is the signal worth having — it is only its parts that are not.
        if entry.domain == "alarm_control_panel":
            continue
        # Enum entities are exempt: the filter already admits only those, and Reolink marks
        # its day/night state as diagnostic — which is fair from a camera's point of view and
        # wrong from here, since it is the best signal the camera has.
        if entry.entity_category is not None and entry.domain not in RELEVANCE_SIGNAL_ENUM_DOMAINS:
            hidden.append(entry.entity_id)
            continue
        # The state as well as the registry: an integration that sets its device class at
        # runtime rather than at registration leaves the registry's copy empty, and Starlink's
        # "update" and "connectivity" walked straight through a registry-only check.
        state = hass.states.get(entry.entity_id)
        device_class = (
            entry.device_class
            or entry.original_device_class
            or (state.attributes.get("device_class") if state else None)
        )
        if device_class in _SELF_REPORTING:
            hidden.append(entry.entity_id)
            continue
        if entry.config_entry_id in ours or entry.entity_id in (detections | automatic):
            hidden.append(entry.entity_id)
            continue
        if entry.device_id is not None and entry.device_id in parts:
            hidden.append(entry.entity_id)
    return hidden
