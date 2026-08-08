"""What the "learn what is normal" beta makes of a camera.

Three questions, and they are different sizes. What fired inside one recording; what the
model makes of every event in a window; and what a camera has learned overall, which is the
counterpart of the second — that one says why an event stood out, this one says what it stood
out from.

All three answer while a camera is still collecting. The scores mean nothing yet, but what
has been *collected* does, and showing it is the only way somebody who does not read Python
can tell whether the feature is working.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from ..const import DOMAIN
from ..detections import async_detections_in_window
from ..relevance.journal import camera_key
from ..relevance.score import SCORE_MIN_DAYS, SCORE_MIN_EVENTS
from ..relevance.shapes import profile_payload
from ..relevance.watcher import async_signal_map
from ..reolink_registry import async_discover_devices
from .shared import TARGET_SCHEMA, _access

_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/detections",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
    }
)
@websocket_api.async_response
async def ws_detections(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the exact moments detections fired inside a recording.

    The device only tags a whole segment, so this comes from Home Assistant's recorder
    instead. It is what lets playback open just before the event rather than at the start
    of a five-minute clip.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    try:
        start = dt.datetime.fromisoformat(msg["start"])
        end = dt.datetime.fromisoformat(msg["end"])
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    detections = await async_detections_in_window(hass, msg["entry_id"], msg["channel"], start, end)
    connection.send_result(
        msg["id"],
        {
            "detections": detections,
            # Where to put the playhead, and — separately — how far either side of the
            # detections the clip itself extends.
            "lead": data.options.event_lead,
            "clip_lead": data.options.clip_lead,
            "clip_tail": data.options.clip_tail,
        },
    )


# -------------------------------------------------------------------- relevance


@callback
def _camera_names(hass: HomeAssistant, include_all_devices: bool) -> dict[str, str]:
    """Map each camera's journal key onto the name a person would recognise.

    The scorer deliberately knows nothing about names — it is handed them so its sentences
    read as English rather than as identifiers, and works without them if the registry has
    nothing to say.
    """
    return {
        camera_key(device.entry_id, camera.channel): camera.name
        for device in async_discover_devices(hass, include_all_devices=include_all_devices)
        for camera in device.cameras
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/relevance",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
    }
)
@websocket_api.async_response
async def ws_relevance(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return what has been learned about one camera, and what it makes of each event.

    Deliberately answers while a camera is still collecting. The scores mean nothing yet and
    nothing is marked — but *what was collected* is worth showing from the first day, and it
    is the only way somebody who does not read Python can tell whether this is working.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    if data.relevance is None:
        connection.send_result(msg["id"], {"enabled": False})
        return

    try:
        start = dt.datetime.fromisoformat(msg["start"])
        end = dt.datetime.fromisoformat(msg["end"])
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    analysis = data.relevance.analysis
    camera = camera_key(msg["entry_id"], msg["channel"])
    names = _camera_names(hass, data.options.beta_all_devices)

    # What Home Assistant calls each chosen entity, so a term reads "Someone home — off"
    # rather than an entity id. The scorer is handed them; it knows nothing about entities.
    labels = {
        entity_id: (state.name if (state := hass.states.get(entity_id)) else entity_id)
        for entities in (data.options.relevance_signals or {}).values()
        for entity_id in entities
    }
    scored = await analysis.async_window(
        since=start.timestamp(),
        until=end.timestamp(),
        camera=camera,
        names=names,
        labels=labels,
    )
    connection.send_result(
        msg["id"],
        {
            "enabled": True,
            # "collecting", "too_few_events" or "active" — the middle one is the camera that
            # has months of days behind it and still too little to compare against.
            "state": analysis.state(camera),
            "coverage": analysis.coverage(camera),
            # Sent rather than hardcoded in the panel, so it can say which requirement is
            # actually outstanding instead of listing both and being wrong about one.
            "needs": {"days": SCORE_MIN_DAYS, "events": SCORE_MIN_EVENTS},
            "events": [
                {
                    "at": dt_util.utc_from_timestamp(event.started_at).isoformat(),
                    "kind": event.kind,
                    "duration": event.duration,
                    "score": round(result.total, 2),
                    "threshold": (None if result.threshold is None else round(result.threshold, 2)),
                    "unusual": result.unusual,
                    "reason": result.reason,
                    "terms": [
                        {
                            "name": term.name,
                            "subject": term.subject,
                            "label": term.label,
                            "contribution": round(term.contribution, 2),
                            "seen": term.seen,
                        }
                        for term in result.terms
                    ],
                }
                for event, result in scored
            ],
        },
    )


# --------------------------------------------------------- what a camera learned


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/relevance_profile",
        vol.Required("targets"): vol.All(cv.ensure_list, [TARGET_SCHEMA]),
    }
)
@callback
def ws_relevance_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return what one camera has learned, as distributions the panel can draw.

    The counterpart of the per-event breakdown, and arguably the more useful half: that one
    says why *this* event stood out, and this one says what it stood out from. It is also the
    only way to see that a camera has learned something wrong — a week of scaffolding outside,
    a sensor that flapped for a fortnight — which is otherwise invisible until it silently
    stops marking anything.

    Straight off the nightly model. Nothing is computed here that scoring does not already
    compute, so this stays a lookup no matter how much history is behind it.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return
    if data.relevance is None:
        connection.send_result(msg["id"], {"enabled": False})
        return

    analysis = data.relevance.analysis
    cameras = [camera_key(t["entry_id"], t["channel"]) for t in msg["targets"]]
    if not cameras:
        connection.send_error(
            msg["id"], websocket_api.const.ERR_INVALID_FORMAT, "No cameras asked about"
        )
        return
    names = _camera_names(hass, data.options.beta_all_devices)
    # Every signal on this camera, chosen or discovered — the floodlight and the day/night
    # state are attached automatically and would otherwise show as bare entity ids.
    watched = async_signal_map(
        hass,
        data.options.relevance_signals or {},
        include_all_devices=data.options.beta_all_devices,
    )
    labels = {
        entity_id: (state.name if (state := hass.states.get(entity_id)) else entity_id)
        for camera in cameras
        for entity_id in watched.get(camera, ())
    }

    # Across several cameras the state is the *least* ready of them, and the coverage is the
    # longest span with every camera's events in it. Reporting the best of them would say the
    # overview is active while half of what it draws is still a fortnight of nothing.
    order = ("collecting", "too_few_events", "active")
    states = [analysis.state(camera) for camera in cameras]
    coverages = [analysis.coverage(camera) for camera in cameras]

    connection.send_result(
        msg["id"],
        {
            "enabled": True,
            "state": min(states, key=order.index),
            "coverage": {
                "days": max((c["days"] for c in coverages), default=0.0),
                "events": sum(c["events"] for c in coverages),
            },
            **profile_payload(analysis.model, cameras, names=names, labels=labels),
        },
    )
