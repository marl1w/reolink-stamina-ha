"""Browsing recordings: the event list, and which days have anything on them.

Both are *subscriptions* rather than request/response, and that is what makes the panel feel
instant against a slow recorder. Whatever is cached goes back immediately as a snapshot,
however stale, each bucket carrying its age and whether a refresh is running; refreshes then
run in the background and every result is pushed as a patch for that one camera-day.

The panel therefore paints at once and fills in as the device answers. A slow or offline
recorder degrades the freshness of the data, never the responsiveness of the UI.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from ..cache import _age, day_key
from ..const import DOMAIN, STREAM_MAIN, STREAM_SUB
from ..reolink_registry import (
    async_discover_devices,
)
from ..vod import build_events, is_continuous_day
from .shared import (
    MAX_BUCKETS,
    TARGET_SCHEMA,
    _access,
    _clamp_range,
    _dates_in,
    _parse_date,
    _secondary_stream,
)

_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/events",
        vol.Required("targets"): vol.All(cv.ensure_list, [TARGET_SCHEMA]),
        vol.Required("start_date"): cv.string,
        vol.Required("end_date"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
    }
)
@callback
def ws_events(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to event rows for a set of cameras over a date range."""
    data = _access(hass, connection, msg)
    if data is None:
        return

    cache = data.cache
    options = data.options

    try:
        start = _parse_date(msg["start_date"])
        end = _parse_date(msg["end_date"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    start, end = _clamp_range(start, end)
    dates = _dates_in(start, end)

    # Resolve camera metadata once; needed for names and pre-roll marker placement.
    devices = {
        device.entry_id: device
        for device in async_discover_devices(hass, include_all_devices=options.beta_all_devices)
    }

    primary = options.browse_stream
    secondary = _secondary_stream(primary)

    buckets: list[tuple[str, int, dt.date]] = []
    for target in msg["targets"]:
        entry_id = target["entry_id"]
        channel = target["channel"]
        device = devices.get(entry_id)
        if device is None or device.status != "ok":
            continue
        for date in dates:
            buckets.append((entry_id, channel, date))

    truncated = len(buckets) > MAX_BUCKETS
    if truncated:
        buckets = buckets[:MAX_BUCKETS]

    @callback
    def _camera_of(entry_id: str, channel: int) -> dict[str, Any] | None:
        device = devices.get(entry_id)
        if device is None:
            return None
        for camera in device.cameras:
            if camera.channel == channel:
                return camera.as_dict()
        return None

    @callback
    def _compose(entry_id: str, channel: int, date: dt.date) -> dict[str, Any]:
        """Build the payload for one camera-day from whatever is cached."""
        device = devices.get(entry_id)
        camera = _camera_of(entry_id, channel)

        unlabelled_wanted = options.include_unlabelled
        primary_record = cache.peek_day(entry_id, channel, primary, date, unlabelled_wanted)
        secondary_record = cache.peek_day(entry_id, channel, secondary, date, unlabelled_wanted)

        # Whatever the other resolution happens to be cached already is free to use, but
        # nothing is fetched for it here.
        other_files: dict[str, list[dict[str, Any]]] = {}
        if secondary_record is not None:
            other_files[secondary] = secondary_record.files

        # Derived from what is cached rather than re-measured: the search already made this
        # judgement, and the count of recordings it discarded is the tell (they are only
        # ever discarded from a continuous camera).
        continuous = (
            is_continuous_day(primary_record.files, date, unlabelled=primary_record.unlabelled)
            if primary_record is not None
            else False
        )

        events = build_events(
            entry_id=entry_id,
            device_name=device.name if device else entry_id,
            channel=channel,
            camera=camera,
            primary_stream=primary,
            primary_files=primary_record.files if primary_record else [],
            other_files=other_files,
            pre_roll_default=options.pre_roll,
            # Only the two real resolutions: autotrack variants are not reliably
            # present, and offering one that cannot play is worse than not offering it.
            alternate_streams=[
                name
                for name in ((camera or {}).get("streams") or [])
                if name in (STREAM_MAIN, STREAM_SUB)
            ],
            continuous=continuous,
        )

        age: float | None = None
        if primary_record is not None and primary_record.fetched_at:
            measured = _age(primary_record.fetched_at)
            age = None if measured == float("inf") else round(measured, 1)

        primary_key = day_key(entry_id, channel, primary, date, unlabelled_wanted)

        return {
            "key": f"{entry_id}|{channel}|{date.isoformat()}",
            "entry_id": entry_id,
            "channel": channel,
            "date": date.isoformat(),
            "events": events,
            # Never cached yet, so the panel can tell "empty day" from "not looked yet".
            "loaded": primary_record is not None,
            "age": age,
            "error": primary_record.error if primary_record else None,
            "updating": cache.async_is_fetching(primary_key),
            # Continuous footage discarded before it was ever stored or sent, so the
            # panel can say so rather than silently showing a short list.
            "unlabelled_skipped": primary_record.unlabelled if primary_record else 0,
        }

    # 1. Answer immediately from cache.
    try:
        composed = [_compose(*bucket) for bucket in buckets]
    except Exception as err:
        _LOGGER.exception("Building the event snapshot failed")
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNKNOWN_ERROR,
            f"Could not build the event list: {err}",
        )
        return

    snapshot = {
        "type": "snapshot",
        "primary_stream": primary,
        "secondary_stream": secondary,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "truncated": truncated,
        "buckets": composed,
    }

    bucket_index = {
        f"{entry_id}|{channel}|{date.isoformat()}": (entry_id, channel, date)
        for entry_id, channel, date in buckets
    }

    @callback
    def _on_cache_update(cache_key: str) -> None:
        """Push a patch when a camera-day this subscription cares about changes."""
        parts = cache_key.split("|")
        if len(parts) not in (4, 5):
            return  # a calendar key, not ours
        entry_id, channel_str, _stream, date_str = parts[:4]
        bucket_key = f"{entry_id}|{channel_str}|{date_str}"
        bucket = bucket_index.get(bucket_key)
        if bucket is None:
            return
        connection.send_message(
            websocket_api.event_message(msg["id"], {"type": "patch", "bucket": _compose(*bucket)})
        )

    connection.subscriptions[msg["id"]] = cache.async_add_listener(_on_cache_update)
    connection.send_result(msg["id"])
    connection.send_message(websocket_api.event_message(msg["id"], snapshot))

    # 2. Refresh in the background, newest day first, since that is what the user is most
    #    likely looking at.
    #
    #    One search per camera-day, and only in the resolution being browsed. Searching
    #    the other resolution as well used to double the load on the recorder purely so a
    #    row could show which qualities exist; it is now resolved on demand, when a
    #    different quality is actually played.
    force = msg["force"]
    for entry_id, channel, date in buckets:
        cache.async_ensure_day(
            entry_id,
            channel,
            primary,
            date,
            options.split_minutes,
            include_unlabelled=options.include_unlabelled,
            force=force,
        )


# ----------------------------------------------------------------------- calendar


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/calendar",
        vol.Required("targets"): vol.All(cv.ensure_list, [TARGET_SCHEMA]),
        vol.Required("year"): vol.Coerce(int),
        vol.Required("month"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
        vol.Optional("force", default=False): cv.boolean,
    }
)
@callback
def ws_calendar(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to which days of a month hold recordings, per camera."""
    data = _access(hass, connection, msg)
    if data is None:
        return

    cache = data.cache
    year: int = msg["year"]
    month: int = msg["month"]

    targets = [(target["entry_id"], target["channel"]) for target in msg["targets"]]

    @callback
    def _compose(entry_id: str, channel: int) -> dict[str, Any]:
        record = cache.peek_calendar(entry_id, channel, year, month)
        return {
            "key": f"{entry_id}|{channel}",
            "entry_id": entry_id,
            "channel": channel,
            "days": record.days if record else [],
            "loaded": record is not None,
            "error": record.error if record else None,
        }

    snapshot = {
        "type": "snapshot",
        "year": year,
        "month": month,
        "cameras": [_compose(entry_id, channel) for entry_id, channel in targets],
    }

    wanted = {
        f"{entry_id}|{channel}|{year:04d}-{month:02d}": (entry_id, channel)
        for entry_id, channel in targets
    }

    @callback
    def _on_cache_update(cache_key: str) -> None:
        target = wanted.get(cache_key)
        if target is None:
            return
        connection.send_message(
            websocket_api.event_message(msg["id"], {"type": "patch", "camera": _compose(*target)})
        )

    connection.subscriptions[msg["id"]] = cache.async_add_listener(_on_cache_update)
    connection.send_result(msg["id"])
    connection.send_message(websocket_api.event_message(msg["id"], snapshot))

    for entry_id, channel in targets:
        cache.async_ensure_calendar(entry_id, channel, year, month, force=msg["force"])
