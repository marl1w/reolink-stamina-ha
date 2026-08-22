"""Getting a picture onto the screen, and hearing about it when that fails.

The route a recording takes to the browser is decided here: the recorder's own bytes where
they will play, and one of the conversions where they will not. What the panel reports back
about a failure is how the next attempt picks a different rung.
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

from ..const import DOMAIN, SEARCH_WINDOW_DAYS
from ..flv_proxy import async_flv_path
from ..reolink_registry import (
    DeviceUnavailableError,
    ReolinkIncompatibleError,
    async_paired_channel,
)
from ..restream import (
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
from ..vod import _overlap_seconds
from .shared import (
    ROUTE_PASSTHROUGH,
    ROUTE_REMUX,
    ROUTE_TRANSCODE,
    _access,
    _parse_date,
)

_LOGGER = logging.getLogger(__name__)


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


@callback
def _async_playback_target(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    source_entry_id: str | None,
    source_channel: int | None,
) -> tuple[str, int]:
    """Return the device to address for playback, refusing to be pointed anywhere else.

    A camera whose recordings live on a recorder has to be played back from that recorder,
    so the row remembers which device answered for it and the panel hands that back here.

    Checked rather than taken. The only device a camera may be served from, other than
    itself, is the recorder channel it is currently paired to -- so a stale row naming a
    channel the user has since taken back into use, or a client naming anything at all,
    falls back to the camera rather than reaching a device nobody asked about.
    """
    if source_entry_id is None or source_channel is None:
        return entry_id, channel
    asked = (source_entry_id, source_channel)
    if asked == (entry_id, channel):
        return entry_id, channel
    if async_paired_channel(hass, entry_id, channel) == asked:
        return asked
    _LOGGER.debug(
        "Ignoring playback source %s for %s channel %s: not its paired channel",
        asked,
        entry_id,
        channel,
    )
    return entry_id, channel


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
        # Which device answered for this row, where that is not the camera itself. Verified
        # against the camera's current pairing rather than trusted.
        vol.Optional("source_entry_id", default=None): vol.Any(None, cv.string),
        vol.Optional("source_channel", default=None): vol.Any(None, vol.Coerce(int)),
        # Seconds from the start of the recording to the start of this row's window. A
        # long recording is split into several rows, all sharing one file, so without
        # this every row replays the file from its beginning.
        vol.Optional("offset", default=0): vol.Coerce(float),
        # Seconds into the recording to start from. Server-side, time-based seeking.
        vol.Optional("seek", default=0): vol.Coerce(int),
        # Which route the panel wants. `passthrough` is the only one that costs the machine
        # nothing; the other two convert.
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

    The `remux` and `transcode` routes are adaptive playback, for a browser that cannot
    play what the recorder sends — see restream.py. Which recording is wanted is resolved
    identically for all three, which is the point of them sharing this command.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    route = msg["route"]
    filename = msg["filename"]
    start_id = msg["start_id"]
    playback_id = msg["playback_id"]
    offset = max(0.0, float(msg["offset"]))
    source_entry_id = msg["source_entry_id"]
    source_channel = msg["source_channel"]

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
        # Resolved from the cache, so the row's own record of who answered is authoritative.
        source_entry_id = resolved.get("source_entry_id")
        source_channel = resolved.get("source_channel")

    # The panel asks in seconds from the start of the row it is showing, which is the only
    # frame of reference it has. The recorder counts from the start of the recording, so
    # the window's own offset is added here rather than being the panel's problem.
    within_window = max(0, int(msg["seek"]))
    seek = int(offset) + within_window

    # Where the bytes come from, which is the camera unless its recordings live elsewhere.
    # `msg["entry_id"]` stays the camera's throughout: it is what the cache is keyed by and
    # what access was granted for, and only the device being addressed moves.
    play_entry_id, play_channel = _async_playback_target(
        hass, msg["entry_id"], msg["channel"], source_entry_id, source_channel
    )

    result: dict[str, Any] = {
        # Echoed back window-relative, matching what the panel displays.
        "seek": within_window,
        # Seeking reopens the stream at a new offset, so it always works.
        "seekable": True,
        "route": route,
    }

    if route == ROUTE_PASSTHROUGH:
        result["path"] = async_flv_path(
            play_entry_id,
            play_channel,
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
                play_entry_id,
                play_channel,
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
        play_entry_id,
        play_channel,
        msg["stream"],
        filename,
        start_id,
        playback_id,
        seek,
        mode,
    )
    result["mime"] = "video/mp4"
    connection.send_result(msg["id"], result)


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


# ------------------------------------------------- has the recorder still got it


# What the panel is told, and it is deliberately three answers rather than two. "Not there"
# and "could not ask" lead to opposite advice, and collapsing them would put the worse of the
# two messages in front of a user whose recorder was merely busy.
STATUS_PRESENT = "present"
STATUS_GONE = "gone"
STATUS_UNKNOWN = "unknown"


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/recording_status",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("stream"): cv.string,
        vol.Required("date"): cv.string,
        vol.Required("filename"): cv.string,
    }
)
@websocket_api.async_response
async def ws_recording_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Say whether the recorder still holds a recording the panel is showing.

    Asked when playback has failed on every route, and it exists because of what the panel
    used to do next: offer to download the clip instead. That is the right answer to a codec
    the browser cannot decode, and exactly the wrong one to a recording the recorder has
    deleted — the download reads the same bytes from the same device, so it fails the same
    way, and being invited to try it reads as a panel that does not know what it is showing.
    A row can outlive its footage by up to a week: a past day is cached for `TTL_PAST` on the
    sound reasoning that it cannot gain recordings, which says nothing about losing them as
    the disk fills.

    The recorder is asked rather than guessed at. A 404 on every playback endpoint looks like
    a deleted recording and is not proof of one — the same 404 came from a timestamp
    convention this integration once got wrong, on files that were all still there — so what
    settles it is whether the day's own listing still names the file. That also repairs the
    cause: the search is forced, so the stale row it was asked about goes with it.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    try:
        date = _parse_date(msg["date"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    # Past what the recorder will search, there is nothing to ask and nothing to play: the
    # search window is the horizon for both. Answered without a request, because a search
    # beyond it comes back empty for a reason that has nothing to do with this recording.
    if (dt_util.now().date() - date).days > SEARCH_WINDOW_DAYS:
        connection.send_result(msg["id"], {"status": STATUS_GONE, "reason": "beyond_search_window"})
        return

    cache = data.cache
    options = data.options
    entry_id = msg["entry_id"]
    channel = msg["channel"]
    stream = msg["stream"]

    task = cache.async_ensure_day(
        entry_id,
        channel,
        stream,
        date,
        options.split_minutes,
        include_unlabelled=options.include_unlabelled,
        force=True,
    )
    if task is not None:
        await task

    record = cache.peek_day(entry_id, channel, stream, date, options.include_unlabelled)
    if record is None or record.error:
        # The device could not be asked. Saying "deleted" here would be a guess dressed up
        # as a finding, and the panel's existing advice is the better answer to a maybe.
        connection.send_result(
            msg["id"],
            {"status": STATUS_UNKNOWN, "reason": (record.error if record else "no answer")},
        )
        return

    # By file name rather than by start id. A long recording is split into rows that share
    # one name, and which boundaries the split lands on depends on settings that may have
    # changed since this row was built -- so a missing start id means "not that slice any
    # more", while a missing name means the recording itself is gone, which is the question.
    filename = msg["filename"]
    listed = any((file.get("name") or "") == filename for file in record.files)
    connection.send_result(
        msg["id"],
        {
            "status": STATUS_PRESENT if listed else STATUS_GONE,
            # What the day holds now, so a panel that wants to say "and 40 others went with
            # it" has the number without asking again.
            "remaining": len(record.files),
        },
    )
