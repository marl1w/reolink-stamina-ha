"""Exact detection times, from Home Assistant's recorder.

The device's own search cannot answer "when did the person appear?". It reports which
triggers occurred somewhere inside a recording segment, and with 24/7 recording that
segment is minutes long — so a person detection means "somewhere in these 300 seconds",
which is tedious to review.

Home Assistant already knows the answer precisely: the Reolink integration exposes a
binary sensor per detection type per camera, and the recorder holds the instant each one
turned on. Reading that back turns a five-minute segment into a handful of exact
timestamps, which is what lets playback open a few seconds before the event instead of at
the start of the clip.

Bounded by the recorder's retention (ten days by default), and by it being enabled at
all — when there is nothing to read, playback simply starts at the beginning as before.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .reolink_registry import async_get_host

_LOGGER = logging.getLogger(__name__)

# Reolink's binary sensor keys, mapped onto the trigger vocabulary the panel uses.
# Anything not listed is ignored rather than guessed at.
#
# The smart-detection sensors are keyed by zone *and* subject — `crossline_dog_cat`,
# `intrusion_person` and so on. They map to the subject, not the zone: a person crossing a
# line is a person, which is both what the timeline should say and what makes cloud sync's
# "person" tick pick the event up. The zone kinds the panel labels (`crossline`,
# `intrusion`, `linger`) come from the recorder's own VOD triggers, not from here.
_SENSOR_KINDS = {
    "person": "person",
    "vehicle": "vehicle",
    "non-motor_vehicle": "vehicle",
    "pet": "animal",
    "dog_cat": "animal",
    "animal": "animal",
    "package": "package",
    "face": "face",
    "visitor": "doorbell",
    "doorbell": "doorbell",
    "cry": "crying",
    "crying": "crying",
    "motion": "motion",
    "crossline_person": "person",
    "crossline_vehicle": "vehicle",
    "crossline_dog_cat": "animal",
    "intrusion_person": "person",
    "intrusion_vehicle": "vehicle",
    "intrusion_dog_cat": "animal",
    "linger_person": "person",
    "linger_vehicle": "vehicle",
    "linger_dog_cat": "animal",
    "forgotten_item": "forgotten_item",
    "taken_item": "taken_item",
    "io_input": "io",
}

# Sensors that exist but are not detections, so finding no kind for them is correct.
# Listed so the contract test can tell "deliberately ignored" from "newly added and
# silently dropped", which is how the `*_dog_cat` animals went missing.
_NOT_DETECTIONS = frozenset({"sleep"})


def _kind_for_key(key: str) -> str | None:
    """Map one Reolink sensor key onto a panel trigger.

    Falls back to the last segment so a zone Reolink adds later — a hypothetical
    `tripwire_person` — still reports its subject instead of vanishing. The fallback is
    what used to run alone, and it is why `crossline_dog_cat` resolved to "cat".
    """
    return _SENSOR_KINDS.get(key) or _SENSOR_KINDS.get(key.rsplit("_", 1)[-1])


@callback
def async_detection_entities(hass: HomeAssistant, entry_id: str, channel: int) -> dict[str, str]:
    """Map entity_id -> detection kind for one camera's detection sensors.

    Channels are addressed either numerically or by camera UID, exactly as elsewhere, so
    both forms have to be resolved or half the cameras find nothing.
    """
    found: dict[str, str] = {}
    try:
        api = async_get_host(hass, entry_id).api
    except Exception:
        return found
    channel_for_uid = getattr(api, "channel_for_uid", None)

    try:
        ent_reg = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
            if entity.domain != "binary_sensor":
                continue
            parts = entity.unique_id.split("_")
            if len(parts) < 3:
                continue

            token = parts[1]
            resolved: int | None = None
            if token.isdigit():
                resolved = int(token)
            elif token.startswith("ch") and token[2:].isdigit():
                resolved = int(token[2:])
            elif channel_for_uid is not None:
                try:
                    resolved = channel_for_uid(token)
                except Exception:
                    resolved = None
            if resolved != channel:
                continue

            # Everything after the channel token is the Reolink entity key, which can
            # itself contain underscores.
            kind = _kind_for_key("_".join(parts[2:]).lower())
            if kind is not None:
                found[entity.entity_id] = kind
    except Exception:
        _LOGGER.debug("Could not list detection sensors", exc_info=True)
    return found


async def _async_history(
    hass: HomeAssistant,
    start: dt.datetime,
    end: dt.datetime,
    entity_ids: list[str],
) -> dict[str, list[Any]]:
    """Read state history, returning nothing at all if the recorder cannot answer.

    The recorder is optional in Home Assistant and may be disabled, still starting, or
    purged. None of that should stop a clip from playing, so every failure here means
    "no detection times known" rather than an error.

    `get_significant_states` is used rather than `state_changes_during_period` because it
    accepts several entities in one query; the latter takes a single entity id, and
    passing a list lands silently in its `limit` argument.
    """
    try:
        from homeassistant.components.recorder import (
            get_instance,
            history,
        )
    except ImportError:
        return {}

    try:
        instance = get_instance(hass)
    except Exception:
        return {}
    if instance is None:
        return {}

    def _read() -> dict[str, list[Any]]:
        return history.get_significant_states(
            hass,
            start,
            end,
            entity_ids,
            None,
            # The state before the window matters: it tells an already-on sensor apart
            # from one that turned on inside it.
            True,
            True,
            False,
            True,
        )

    try:
        return await instance.async_add_executor_job(_read)
    except Exception:
        _LOGGER.debug("Could not read detection history", exc_info=True)
        return {}


async def async_detections_in_window(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    """Return the moments detections fired inside a window, oldest first.

    Only transitions *into* the detected state count: a sensor that was already on when
    the window opened says nothing about when the event began.

    Each detection also carries when it cleared, which is what lets the player play the
    event rather than the whole segment it sits in. A sensor still on at the end of the
    window is reported as lasting to the end of it — the event may well continue into the
    next recording, and claiming it stopped here would be a guess.
    """
    entities = async_detection_entities(hass, entry_id, channel)
    if not entities:
        return []

    # A little slack before the window: a detection just before the segment boundary is
    # what the segment is tagged for.
    lookback = start - dt.timedelta(seconds=30)

    try:
        changes = await _async_history(hass, lookback, end, list(entities))
    except Exception:
        _LOGGER.debug("Detection history unavailable", exc_info=True)
        return []

    def _offset(moment: dt.datetime) -> float:
        # Negative when the detection preceded the segment, which is normal for the
        # segment that a detection is tagged against.
        return round((moment - start).total_seconds(), 1)

    detections: list[dict[str, Any]] = []
    for entity_id, states in (changes or {}).items():
        kind = entities.get(entity_id)
        if kind is None:
            continue
        previous: str | None = None
        open_run: dict[str, Any] | None = None
        for state in states:
            current = state.state
            if current == "on" and previous != "on" and state.last_changed >= lookback:
                open_run = {
                    "at": state.last_changed.isoformat(),
                    "kind": kind,
                    "offset": _offset(state.last_changed),
                }
                detections.append(open_run)
            elif current != "on" and previous == "on" and open_run is not None:
                open_run["until"] = state.last_changed.isoformat()
                open_run["end_offset"] = _offset(state.last_changed)
                open_run = None
            previous = current

        # Still detecting when the history ran out.
        if open_run is not None:
            open_run["until"] = end.isoformat()
            open_run["end_offset"] = _offset(end)

    detections.sort(key=lambda item: item["at"])
    return detections
