"""Websocket API for the Reolink Stamina panel.

The event and calendar commands are *subscriptions*, not request/response, because that
is what makes the panel feel instant against a slow recorder:

1. On subscribe, whatever is cached is sent straight back as a snapshot, however stale,
   each bucket carrying its age and whether a refresh is running.
2. Refreshes run in the background, and every result is pushed as a patch for that one
   camera-day.

The panel therefore paints immediately and fills in as the device answers. A slow or
offline recorder degrades the freshness of the data, never the responsiveness of the UI.
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

from .cache import _age, day_key
from .const import (
    DOMAIN,
    SEARCH_WINDOW_DAYS,
    STREAM_MAIN,
    STREAM_SUB,
)
from .detections import async_detections_in_window
from .flv_proxy import async_flv_path
from .fragments import FragmentsUnsupportedError, async_fragment_path
from .reolink_registry import (
    DeviceUnavailableError,
    ReolinkIncompatibleError,
    async_discover_devices,
)
from .restream import (
    FORMAT_HLS,
    FORMAT_MP4,
    MODE_COPY,
    MODE_ENCODE,
    RESTREAM_FORMATS,
    FfmpegUnavailableError,
    async_get_manager,
    async_hls_path,
    async_restream_path,
    async_start_hls,
)
from .vod import _overlap_seconds, build_events, is_continuous_day

_LOGGER = logging.getLogger(__name__)

# A generous ceiling that still prevents one subscription asking a device for thousands
# of searches.
MAX_BUCKETS = 240

# What the panel may ask `stream_url` for. Passthrough is the recorder's own bytes and the
# only route available with the adaptive beta off; the other two are conversions.
ROUTE_PASSTHROUGH = "passthrough"
ROUTE_REMUX = "remux"
ROUTE_TRANSCODE = "transcode"

TARGET_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
    }
)


@callback
def _runtime(hass: HomeAssistant) -> Any:
    """Return this integration's runtime data, or None if it is not set up."""
    return hass.data.get(DOMAIN)


@callback
def _access(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> Any | None:
    """Return the runtime data if this connection may use the panel, else None.

    Answers both "is the integration still loaded" and "is this user allowed", because
    an open panel can outlive an unload or a reload. The admin check mirrors the panel's
    own option rather than hard-coding admin-only, so the two cannot disagree.
    """
    data = _runtime(hass)
    if data is None:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            "Reolink Stamina is not set up",
        )
        return None

    if data.options.require_admin and not connection.user.is_admin:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNAUTHORIZED,
            "Only administrators may browse Reolink recordings",
        )
        return None

    return data


def _parse_date(value: str) -> dt.date:
    """Parse an ISO date, raising a websocket-friendly error."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as err:
        raise vol.Invalid(f"Invalid date '{value}'") from err


def _clamp_range(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """Clamp a requested range to what the device can actually search."""
    today = dt_util.now().date()
    earliest = today - dt.timedelta(days=SEARCH_WINDOW_DAYS)
    start = max(start, earliest)
    end = min(end, today)
    if end < start:
        end = start
    return start, end


def _dates_in(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every date in an inclusive range, newest first."""
    span = (end - start).days
    return [end - dt.timedelta(days=offset) for offset in range(span + 1)]


@callback
def _secondary_stream(primary: str) -> str:
    """Return the other resolution, used only to work out clip availability."""
    if primary == STREAM_SUB:
        return STREAM_MAIN
    if primary == STREAM_MAIN:
        return STREAM_SUB
    # Autotrack streams have no meaningful counterpart; compare against low res.
    return STREAM_SUB


def async_register(hass: HomeAssistant) -> None:
    """Register every websocket command."""
    websocket_api.async_register_command(hass, ws_devices)
    websocket_api.async_register_command(hass, ws_events)
    websocket_api.async_register_command(hass, ws_calendar)
    websocket_api.async_register_command(hass, ws_stream_url)
    websocket_api.async_register_command(hass, ws_detections)
    websocket_api.async_register_command(hass, ws_clip_url)
    websocket_api.async_register_command(hass, ws_playback_failure)


# ------------------------------------------------------------------------ devices


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/devices"})
@callback
def ws_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the recording devices discovered through the Reolink integration.

    Cheap: everything comes from the already-running Reolink integration, with no call
    to the recorder itself.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    options = data.options
    try:
        devices = [
            device.as_dict()
            for device in async_discover_devices(hass, include_all_devices=options.beta_all_devices)
        ]
    except Exception as err:
        # Without this the panel shows a bare "Unknown error" and the reason is only in
        # the log. Discovery reads a non-public Reolink attribute, so say what broke.
        _LOGGER.exception("Reolink device discovery failed")
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNKNOWN_ERROR,
            f"Could not read the Reolink integration: {err}",
        )
        return

    connection.send_result(
        msg["id"],
        {
            "devices": devices,
            "options": options.as_dict(),
            "search_window_days": SEARCH_WINDOW_DAYS,
        },
    )


# ------------------------------------------------------------------------- events


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


async def _async_resolve_file(
    hass: HomeAssistant,
    data: Any,
    entry_id: str,
    channel: int,
    stream: str,
    start: str,
    end: str,
) -> dict[str, Any] | None:
    """Find the recording covering a time window in a given resolution.

    This is what pays for not searching every resolution up front: the panel asks for a
    quality it has no file name for, and it is resolved here. Served from cache whenever
    possible, so switching quality repeatedly costs the recorder nothing.
    """
    cache = data.cache
    options = data.options
    try:
        date = dt.datetime.fromisoformat(start).date()
    except ValueError:
        return None

    task = cache.async_ensure_day(
        entry_id,
        channel,
        stream,
        date,
        options.split_minutes,
        include_unlabelled=options.include_unlabelled,
    )
    if task is not None:
        await task

    record = cache.peek_day(entry_id, channel, stream, date, options.include_unlabelled)
    if record is None:
        return None

    best: dict[str, Any] | None = None
    best_overlap = 0.0
    for candidate in record.files:
        overlap = _overlap_seconds(start, end, candidate["start"], candidate["end"])
        if overlap > best_overlap:
            best, best_overlap = candidate, overlap
    return best if best_overlap > 0 else None


async def _async_resolve_playback_fields(
    hass: HomeAssistant,
    data: Any,
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start: str,
    end: str,
) -> dict[str, Any] | None:
    """Fill in the fields the playback endpoint needs, refetching if the cache is old.

    A record cached by an earlier version has no playback_id, and playback without it is
    refused by the recorder. Rather than fail, search that day again so the record is
    rewritten in the current shape.
    """
    resolved = await _async_resolve_file(hass, data, entry_id, channel, stream, start, end)
    if resolved is not None and resolved.get("playback_id"):
        return resolved

    try:
        date = dt.datetime.fromisoformat(start).date()
    except ValueError:
        return resolved

    task = data.cache.async_ensure_day(
        entry_id,
        channel,
        stream,
        date,
        data.options.split_minutes,
        include_unlabelled=data.options.include_unlabelled,
        force=True,
    )
    if task is not None:
        await task
    return await _async_resolve_file(hass, data, entry_id, channel, stream, start, end)


# ------------------------------------------------------------------- stream URL


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/stream_url",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("stream"): cv.string,
        # Omit the file name to have it resolved from the time window, which is how the
        # panel plays a resolution it never searched for.
        vol.Optional("filename", default=""): cv.string,
        vol.Optional("start", default=""): cv.string,
        vol.Optional("end", default=""): cv.string,
        # Both required by the playback endpoint; resolved server-side when omitted.
        vol.Optional("start_id", default=""): cv.string,
        vol.Optional("playback_id", default=""): cv.string,
        # Seconds from the start of the recording to the start of this row's window. A
        # long recording is split into several rows, all sharing one file, so without
        # this every row replays the file from its beginning.
        vol.Optional("offset", default=0): vol.Coerce(float),
        # Seconds into the recording to start from. Server-side, time-based seeking.
        vol.Optional("seek", default=0): vol.Coerce(int),
        # Which route the panel wants. `passthrough` is the only one that costs the machine
        # nothing, and the only one available with the adaptive beta off.
        vol.Optional("route", default=ROUTE_PASSTHROUGH): vol.In(
            (ROUTE_PASSTHROUGH, ROUTE_REMUX, ROUTE_TRANSCODE)
        ),
        # Which container a converted stream should arrive in, decided by what the browser
        # asking can play.
        vol.Optional("format", default=FORMAT_MP4): vol.In(RESTREAM_FORMATS),
    }
)
@websocket_api.async_response
async def ws_stream_url(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a path that streams a recording, starting part-way in if asked.

    By default the path points at this integration's own pass-through view: the recorder
    already serves a container the browser can demux, so nothing is transcoded, segmented
    or processed server-side. Seeking is done by asking for a different offset.

    The `remux` and `transcode` routes are the adaptive beta, for a browser that cannot
    play what the recorder sends — see restream.py. Which recording is wanted is resolved
    identically for all three, which is the point of them sharing this command.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    route = msg["route"]
    if route != ROUTE_PASSTHROUGH and not data.options.beta_restream:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_SUPPORTED,
            "Adaptive playback is not enabled for this integration",
        )
        return

    filename = msg["filename"]
    start_id = msg["start_id"]
    playback_id = msg["playback_id"]
    offset = max(0.0, float(msg["offset"]))

    if not filename or not start_id or not playback_id:
        resolved = await _async_resolve_playback_fields(
            hass,
            data,
            msg["entry_id"],
            msg["channel"],
            msg["stream"],
            filename,
            msg["start"],
            msg["end"],
        )
        if resolved is None:
            connection.send_error(
                msg["id"],
                websocket_api.const.ERR_NOT_FOUND,
                "This event has no recording in that resolution",
            )
            return
        filename = resolved["name"]
        # The recording's own start, not the window's: the endpoint locates the file.
        start_id = resolved.get("file_start_id") or resolved["start_id"]
        playback_id = resolved.get("playback_id", "")
        offset = max(0.0, float(resolved.get("offset") or 0.0))

    # The panel asks in seconds from the start of the row it is showing, which is the only
    # frame of reference it has. The recorder counts from the start of the recording, so
    # the window's own offset is added here rather than being the panel's problem.
    within_window = max(0, int(msg["seek"]))
    seek = int(offset) + within_window

    result: dict[str, Any] = {
        # Echoed back window-relative, matching what the panel displays.
        "seek": within_window,
        # Seeking reopens the stream at a new offset, so it always works.
        "seekable": True,
        "route": route,
    }

    if route == ROUTE_PASSTHROUGH:
        result["path"] = async_flv_path(
            msg["entry_id"],
            msg["channel"],
            msg["stream"],
            filename,
            start_id,
            playback_id,
            seek,
        )
        result["mime"] = "video/x-flv"
        connection.send_result(msg["id"], result)
        return

    mode = MODE_COPY if route == ROUTE_REMUX else MODE_ENCODE

    if msg["format"] == FORMAT_HLS:
        # Started here rather than on the first request, because what an iPhone is handed
        # has to be a playlist it can fetch without following anything or signing anything.
        try:
            token = await async_start_hls(
                hass,
                msg["entry_id"],
                msg["channel"],
                msg["stream"],
                filename,
                start_id,
                playback_id,
                seek,
                mode,
            )
        except FfmpegUnavailableError as err:
            connection.send_error(msg["id"], websocket_api.const.ERR_NOT_SUPPORTED, str(err))
            return
        except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
            connection.send_error(msg["id"], websocket_api.const.ERR_NOT_FOUND, str(err))
            return
        except Exception as err:
            _LOGGER.exception("Could not start an HLS session")
            connection.send_error(
                msg["id"],
                websocket_api.const.ERR_UNKNOWN_ERROR,
                f"Could not start the conversion: {err}",
            )
            return
        result["path"] = async_hls_path(token)
        result["mime"] = "application/vnd.apple.mpegurl"
        # Its own token is what authorises it; signing would be dropped by the segments.
        result["sign"] = False
        connection.send_result(msg["id"], result)
        return

    result["path"] = async_restream_path(
        msg["entry_id"],
        msg["channel"],
        msg["stream"],
        filename,
        start_id,
        playback_id,
        seek,
        mode,
    )
    result["mime"] = "video/mp4"
    connection.send_result(msg["id"], result)


# ------------------------------------------------------------------- detections


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


# ---------------------------------------------------------------- clip download


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/clip_url",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("stream"): cv.string,
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
    }
)
@callback
def ws_clip_url(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a path that serves exactly this stretch of footage as an MP4.

    The recorder cuts it — see fragments.py. Unsigned, like the playback path: the panel
    signs it with Home Assistant's own command so a plain download link can fetch it.
    """
    if _access(hass, connection, msg) is None:
        return

    try:
        start = dt.datetime.fromisoformat(msg["start"])
        end = dt.datetime.fromisoformat(msg["end"])
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    try:
        path = async_fragment_path(msg["entry_id"], msg["channel"], msg["stream"], start, end)
    except FragmentsUnsupportedError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_NOT_SUPPORTED, str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    connection.send_result(msg["id"], {"path": path, "mime": "video/mp4"})


# --------------------------------------------------------------- why playback failed


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/playback_failure"})
@callback
def ws_playback_failure(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return why the most recent conversion produced nothing.

    Asked by the panel once its ladder is exhausted, because it has no other way to find
    out: a converted route is a URL handed to a `<video>` element, and a 502 reaches the
    browser as a numeric `MediaError` with the explanation thrown away. The whole history
    goes out too, so a person filing a bug can paste something worth reading.
    """
    failures = list(async_get_manager(hass).failures)
    connection.send_result(
        msg["id"], {"failure": failures[-1] if failures else None, "history": failures}
    )
