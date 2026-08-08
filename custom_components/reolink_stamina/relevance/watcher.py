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
from ..detections import async_detection_entities
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


class TransitionWatcher:
    """Subscribes to every detection sensor and hands what they do to the journal."""

    def __init__(
        self, hass: HomeAssistant, journal: Journal, *, include_all_devices: bool = False
    ) -> None:
        """Prepare a watcher, without subscribing."""
        self._hass = hass
        self._journal = journal
        self._include_all_devices = include_all_devices
        self._entities: dict[str, tuple[str, str]] = {}
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
                # `last_changed`, not `last_updated`: the moment the state became this, which
                # is the moment the camera saw something. `last_updated` moves on every
                # attribute refresh and would scatter one detection across several instants.
                at=new_state.last_changed.timestamp(),
                source=JOURNAL_SOURCE_LIVE,
            )
        )
