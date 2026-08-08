"""Writing down detections as they happen.

The panel reads detection times out of Home Assistant's recorder, which works and is fragile
in two ways it cannot do anything about: the recorder is optional and may be switched off,
and it purges — ten days by default. Both show up as detections that silently stop existing.

Listening to the state machine directly has neither problem. A sensor turning on is an event
in Home Assistant whether or not anything is recording it, so from the moment this is running
the journal is complete regardless of what the recorder is configured to keep. The recorder
is then needed exactly once, to import whatever history it still holds; see `backfill`.

Every transition is written, including `unavailable` and `unknown`. They are not detections,
but they are the difference between "this camera saw nothing for six hours" and "this camera
was not connected for six hours", and a rate model that cannot tell those apart will
confidently report that nothing ever happens on a camera that has been offline for a week.
"""

from __future__ import annotations

import json
import logging

from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from ..const import JOURNAL_SOURCE_LIVE
from ..detections import async_camera_signal_entities, async_detection_entities
from ..reolink_registry import async_discover_devices
from .journal import Journal, Transition, camera_key

_LOGGER = logging.getLogger(__name__)


@callback
def async_detection_map(
    hass: HomeAssistant, *, include_all_devices: bool = False
) -> dict[str, tuple[str, str]]:
    """Map every detection sensor onto the camera it belongs to and what it detects.

    Built from discovery rather than from a saved list, so a camera added to a recorder is
    picked up by the next reload without anyone reconfiguring anything. Shared with the
    import path, which has to ask the same question of the same entities.
    """
    found: dict[str, tuple[str, str]] = {}
    for device in async_discover_devices(hass, include_all_devices=include_all_devices):
        for camera in device.cameras:
            key = camera_key(device.entry_id, camera.channel)
            entities = async_detection_entities(hass, device.entry_id, camera.channel)
            for entity_id, kind in entities.items():
                found[entity_id] = (key, kind)
    return found


def async_signal_map(
    hass: HomeAssistant,
    signals: dict[str, list[str]],
    *,
    include_all_devices: bool = False,
) -> dict[str, list[str]]:
    """Map every camera onto the entities whose state is recorded alongside its detections.

    Two sources, and they answer different questions. What the user chose is *per recorder*,
    because that is the granularity at which a household fact is true: whether anybody is in
    says the same thing about every camera on the property.

    What the camera reports about itself is *per camera*, and is not configured at all. A
    floodlight belongs to one lens, and asking somebody to pick thirteen day/night sensors
    from a list — then attaching all thirteen to all thirteen cameras, so every event carried
    twelve terms about other rooms — would be a worse answer than discovering them, which is
    already how the detection sensors themselves are found.

    One function, used by the live watcher and by the import that reconstructs history. Two
    readers disagreeing about which signals belong to a camera would show up only as counts
    that do not add up.
    """
    found: dict[str, list[str]] = {}
    for device in async_discover_devices(hass, include_all_devices=include_all_devices):
        chosen = signals.get(device.entry_id) or []
        for camera in device.cameras:
            own = async_camera_signal_entities(hass, device.entry_id, camera.channel)
            together = sorted({*chosen, *own})
            if together:
                found[camera_key(device.entry_id, camera.channel)] = together
    return found


class TransitionWatcher:
    """Subscribes to every detection sensor and hands what they do to the journal."""

    def __init__(
        self,
        hass: HomeAssistant,
        journal: Journal,
        *,
        include_all_devices: bool = False,
        signals: dict[str, list[str]] | None = None,
    ) -> None:
        """Prepare a watcher, without subscribing.

        `signals` maps a recorder's config entry to the entities whose state should be
        recorded alongside its detections.
        """
        self._hass = hass
        self._journal = journal
        self._include_all_devices = include_all_devices
        self._signals = signals or {}
        self._entities: dict[str, tuple[str, str]] = {}
        # Which signals belong to which camera, worked out once at start rather than per
        # transition: a busy recorder fires several a second.
        self._watching: dict[str, list[str]] = {}
        self._unsubscribe: CALLBACK_TYPE | None = None

    @property
    def watching(self) -> int:
        """Return how many sensors are being listened to."""
        return len(self._entities)

    @callback
    def async_start(self) -> None:
        """Resolve the sensors and start listening.

        Finding none is normal rather than an error: the Reolink integration may not have
        finished setting up, or a recorder may be offline. The next reload tries again, and
        in the meantime nothing is lost that was ever available to be lost.
        """
        self.async_stop()
        self._entities = async_detection_map(
            self._hass, include_all_devices=self._include_all_devices
        )
        self._watching = async_signal_map(
            self._hass, self._signals, include_all_devices=self._include_all_devices
        )
        if not self._entities:
            _LOGGER.debug("No Reolink detection sensors found; the journal has nothing to watch")
            return

        self._unsubscribe = async_track_state_change_event(
            self._hass, list(self._entities), self._async_state_changed
        )
        _LOGGER.debug("Journal watching %s detection sensors", len(self._entities))

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._entities = {}

    @callback
    def _snapshot(self, camera: str) -> str | None:
        """Return the configured signals' states right now, as JSON.

        Raw states, never bucketed. What a value *means* is a question for whoever reads it
        back, so a bucketing can be reconsidered against months of history rather than
        deciding, once and permanently, what was worth writing down.

        An entity that has gone missing is recorded as `unknown` rather than skipped: a
        signal that is absent for half the history is still a fact about that half, and a
        hole would silently shift every count that mentions it.
        """
        entities = self._watching.get(camera)
        if not entities:
            return None
        found = {}
        for entity_id in entities:
            state = self._hass.states.get(entity_id)
            found[entity_id] = state.state if state is not None else "unknown"
        return json.dumps(found, separators=(",", ":"))

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Record one state change."""
        new_state = event.data.get("new_state")
        if new_state is None:
            # The entity was removed. Nothing happened in front of the camera.
            return

        old_state = event.data.get("old_state")
        if old_state is not None and old_state.state == new_state.state:
            # An attribute moved, not the state. Reolink sensors carry attributes that update
            # far more often than they fire, and counting those as detections would drown the
            # real ones several times over.
            return

        known = self._entities.get(new_state.entity_id)
        if known is None:
            return
        camera, kind = known

        self._journal.async_record(
            Transition(
                camera=camera,
                entity_id=new_state.entity_id,
                kind=kind,
                state=new_state.state,
                context=self._snapshot(camera),
                # `last_changed`, not `last_updated`: the moment the state became this, which
                # is the moment the camera saw something. `last_updated` moves on every
                # attribute refresh and would scatter one detection across several instants.
                at=new_state.last_changed.timestamp(),
                source=JOURNAL_SOURCE_LIVE,
            )
        )
