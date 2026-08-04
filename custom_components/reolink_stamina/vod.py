"""Recording search, event composition and playback URLs.

Talks to the NVR through the live reolink_aio object and turns its answers into plain
dictionaries. Nothing here caches; see cache.py. Nothing here filters by trigger type
either — the panel does that client-side so toggling a filter is instant.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONTINUOUS_COVERAGE, STREAM_MAIN, STREAM_SUB
from .nvr_registry import async_get_host

_LOGGER = logging.getLogger(__name__)

# Mirrors the Reolink integration's own media source, which splits long continuous
# recordings so that trigger information stays meaningful per segment.
# Without it, triggers on a long continuous file are OR-ed together and useless.
_MIN_SPLIT_MINUTES = 1
_MAX_SPLIT_MINUTES = 60


def _day_bounds(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return naive start/end datetimes covering a whole day.

    Naive on purpose: reolink_aio applies the device's own timezone, and this is how
    the Reolink integration queries it too.
    """
    start = dt.datetime(date.year, date.month, date.day, 0, 0, 0)
    end = dt.datetime(date.year, date.month, date.day, 23, 59, 59)
    return start, end


def is_continuous_day(files: list[dict[str, Any]], date: dt.date, unlabelled: int = 0) -> bool:
    """Whether this camera-day looks like 24/7 recording rather than event recording.

    The recorder does not say which mode a camera is in, but it can be measured: continuous
    recording covers the whole day, event recording covers almost none of it.

    `unlabelled` short-circuits it, and has to: unlabelled recordings are *only* ever
    discarded from a continuous camera, so a day that dropped any is continuous by
    definition — and its remaining files no longer cover the day, which would otherwise
    make it measure as event recording.
    """
    if unlabelled > 0:
        return True
    start, end = _day_bounds(date)
    recorded = sum(item.get("duration") or 0.0 for item in files)
    day_end = min(dt_util.now().replace(tzinfo=None), end)
    elapsed = max(1.0, (day_end - start).total_seconds())
    return (recorded / elapsed) >= CONTINUOUS_COVERAGE


def trigger_names(triggers: Any) -> list[str]:
    """Decompose a VOD_trigger IntFlag into lowercase names.

    Iterating an IntFlag yields its set members, so this needs no knowledge of the bit
    values, which are auto() and therefore not a stable API.
    """
    if not triggers:
        return []
    names: list[str] = []
    try:
        for member in triggers:
            name = getattr(member, "name", None)
            if name and name != "NONE":
                names.append(name.lower())
    except TypeError:
        # Not iterable (very old reolink_aio); fall back to the flag's own name.
        name = getattr(triggers, "name", None)
        if name and name != "NONE":
            names.append(name.lower())
    return names


def serialize_file(file: Any) -> dict[str, Any]:
    """Turn a reolink_aio VOD_file into a JSON-safe dict.

    Splitting is why this is more involved than it looks. Recorders write long files -- half
    an hour on the hardware tested -- and `split_time` cuts them into rows by rewriting
    StartTime and EndTime only. `PlaybackTime` and the file name are copied from the parent
    unchanged, and those two are what the playback endpoint actually locates a recording by.
    Asking for a window at 08:50 therefore replays the file from 08:30 unless the position
    inside the file is stated separately.

    So both are recorded: the file's own start, which identifies the recording, and this
    window's offset into it, which is where playback should begin.
    """
    start = file.start_time
    end = file.end_time

    # PlaybackTime is the *file's* start in UTC and survives splitting, so it is the only
    # reliable anchor for where this window sits inside the recording.
    playback_time = file.playback_time
    file_start = playback_time.astimezone(start.tzinfo)
    offset = max(0.0, (start - file_start).total_seconds())

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        # Identifies this window: used for event ids and for matching across resolutions.
        "start_id": file.start_time_id,
        "end_id": file.end_time_id,
        # Identifies the recording itself, which is what playback needs.
        "file_start_id": file_start.strftime("%Y%m%d%H%M%S"),
        "playback_id": playback_time.strftime("%Y%m%d%H%M%S"),
        # Seconds from the start of the recording to the start of this window.
        "offset": offset,
        "name": file.file_name,
        "size": file.size,
        "type": file.type,
        "triggers": trigger_names(file.triggers),
        "duration": max(0.0, (end - start).total_seconds()),
    }


async def async_search_day(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    stream: str,
    date: dt.date,
    split_minutes: int,
    include_unlabelled: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Search one camera's recordings for one day on one stream.

    Returns the recordings worth keeping, and how many unlabelled ones were discarded.

    A recorder with 24/7 recording enabled reports continuous footage with no trigger
    flag at all, and it dominates the results — 192 unlabelled segments against 6 real
    detections in one day on the hardware this was tested on. Those are dropped here,
    the earliest point we control, so they are never serialised, never persisted to
    .storage, never sent over the websocket and never turned into rows.

    Note this cannot save the NVR any work: reolink_aio's own `trigger=` argument runs
    the same single search and then returns one bucket from it, so filtering per trigger
    would mean more round trips, not fewer. What it saves is everything after that.
    """
    host = async_get_host(hass, entry_id)
    start, end = _day_bounds(date)

    split_time: dt.timedelta | None = None
    if split_minutes:
        clamped = min(max(split_minutes, _MIN_SPLIT_MINUTES), _MAX_SPLIT_MINUTES)
        split_time = dt.timedelta(minutes=clamped)

    _LOGGER.debug(
        "Searching %s channel %s %s stream on %s",
        entry_id,
        channel,
        stream,
        date.isoformat(),
    )
    _, vod_files = await host.api.request_vod_files(
        channel,
        start,
        end,
        stream=stream,
        split_time=split_time,
    )

    serialised = [serialize_file(file) for file in vod_files]

    # Whether an unlabelled recording is filler or the event itself depends entirely on
    # how the camera records, and the recorder does not say which. Dropping unlabelled
    # recordings from an event-recording camera would hide every event it has, which is
    # exactly what happened to a camera whose NVR reports no event type at all.
    #
    # Measured against every file the search returned, before any are discarded.
    continuous = is_continuous_day(serialised, date)

    # How many rows share each underlying recording, counted over *everything* the search
    # returned. It has to be measured here, before any are discarded: on a 24/7 camera all
    # but a handful of a file's segments are unlabelled, so counting the survivors made a
    # five-minute slice look like the whole recording and claim its full size — 125 MB for
    # 300 seconds of low-resolution footage, on the recorder this was found on.
    segments: dict[str, int] = {}
    for data in serialised:
        name = data.get("name") or ""
        segments[name] = segments.get(name, 0) + 1

    files: list[dict[str, Any]] = []
    unlabelled = 0
    for data in serialised:
        if not data["triggers"] and continuous and not include_unlabelled:
            unlabelled += 1
            continue
        data["segments"] = segments.get(data.get("name") or "", 1)
        files.append(data)

    files.sort(key=lambda item: item["start"])
    if unlabelled:
        _LOGGER.debug(
            "Discarded %s unlabelled continuous recordings for %s channel %s on %s",
            unlabelled,
            entry_id,
            channel,
            date.isoformat(),
        )
    return files, unlabelled


async def async_search_calendar(
    hass: HomeAssistant, entry_id: str, channel: int, year: int, month: int
) -> list[int]:
    """Return the days of a month that contain recordings.

    Uses the NVR's own per-month bitmap, which is one cheap call instead of searching
    every day in detail.
    """
    host = async_get_host(hass, entry_id)

    start = dt.datetime(year, month, 1, 0, 0, 0)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1, 0, 0, 0) - dt.timedelta(seconds=1)
    else:
        end = dt.datetime(year, month + 1, 1, 0, 0, 0) - dt.timedelta(seconds=1)

    statuses, _ = await host.api.request_vod_files(channel, start, end, status_only=True)

    days: set[int] = set()
    for status in statuses or []:
        if status.year != year or status.month != month:
            continue
        days.update(status.days)
    return sorted(days)


def _overlap_seconds(a_start: str, a_end: str, b_start: str, b_end: str) -> float:
    """Seconds of overlap between two ISO intervals."""
    try:
        start = max(dt.datetime.fromisoformat(a_start), dt.datetime.fromisoformat(b_start))
        end = min(dt.datetime.fromisoformat(a_end), dt.datetime.fromisoformat(b_end))
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _pre_roll_for(
    camera: dict[str, Any] | None, default_seconds: int, duration: float
) -> dict[str, Any]:
    """Work out where to put the trigger marker on the scrub bar.

    Exact only when the camera actually reports its pre-record time; otherwise this is
    an estimate and says so, because wired cameras on an NVR generally do not expose
    their pre-alarm setting.
    """
    seconds = float(default_seconds)
    exact = False

    pre_record = (camera or {}).get("pre_record") or {}
    if (
        pre_record.get("supported")
        and pre_record.get("enabled")
        and pre_record.get("seconds") is not None
    ):
        seconds = float(pre_record["seconds"])
        exact = True

    # A marker past the end of the clip would be nonsense.
    if duration > 0:
        seconds = min(seconds, duration)
    return {"seconds": round(max(0.0, seconds), 1), "exact": exact}


def build_events(
    *,
    entry_id: str,
    nvr_name: str,
    channel: int,
    camera: dict[str, Any] | None,
    primary_stream: str,
    primary_files: list[dict[str, Any]],
    other_files: dict[str, list[dict[str, Any]]],
    pre_roll_default: int,
    alternate_streams: list[str] | None = None,
    continuous: bool = False,
) -> list[dict[str, Any]]:
    """Compose event rows from raw per-stream search results.

    The primary stream defines the rows. `other_files` may carry already-known files from
    another resolution, but nothing searches for them on this path: discovering whether a
    clip also exists in high resolution used to cost a second search per camera-day, which
    doubled the load on the recorder to render a badge. `alternate_streams` simply names
    the resolutions the camera supports, and the panel resolves one only if asked to play
    it.
    """
    camera_name = (camera or {}).get("name") or f"Channel {channel}"
    events: list[dict[str, Any]] = []

    # When a long recording is split into segments they all share one file name, and the
    # size the device reports is the whole file's -- so a five-minute segment of 24/7
    # footage claims hundreds of megabytes while an event-triggered clip of the same
    # length claims a few. Counting the occurrences is what tells the two apart.
    #
    # The search stamps that count on each record, because it alone saw the recordings it
    # discarded; counting here would only ever see the survivors. Falling back to counting
    # is for records cached before the field existed, and for callers composing rows by hand.
    segments_per_file: dict[str, int] = {}
    for item in primary_files:
        name = item.get("name") or ""
        segments_per_file[name] = segments_per_file.get(name, 0) + 1

    for file in primary_files:
        duration = float(file.get("duration") or 0.0)
        streams: dict[str, dict[str, Any]] = {
            primary_stream: {
                "name": file["name"],
                "size": file["size"],
                "start_id": file["start_id"],
                "end_id": file["end_id"],
                # Playback addresses the recording, then seeks into it.
                "file_start_id": file.get("file_start_id", ""),
                "playback_id": file.get("playback_id", ""),
                "offset": file.get("offset", 0.0),
            }
        }

        for stream, candidates in other_files.items():
            if stream == primary_stream:
                continue
            best: dict[str, Any] | None = None
            best_overlap = 0.0
            for candidate in candidates:
                overlap = _overlap_seconds(
                    file["start"], file["end"], candidate["start"], candidate["end"]
                )
                if overlap > best_overlap:
                    best, best_overlap = candidate, overlap
            # Require meaningful overlap so a neighbouring segment is not mistaken
            # for this one.
            if best is not None and best_overlap >= min(2.0, max(duration, 1.0) / 2):
                streams[stream] = {
                    "name": best["name"],
                    "size": best["size"],
                    "start_id": best["start_id"],
                    "end_id": best["end_id"],
                    "file_start_id": best.get("file_start_id", ""),
                    "playback_id": best.get("playback_id", ""),
                    "offset": best.get("offset", 0.0),
                }

        # A file reporting zero bytes in every stream is not worth opening a player for.
        playable = any((info.get("size") or 0) > 0 for info in streams.values())

        # Only meaningful when this row is the whole file rather than a slice of it.
        counted = file.get("segments")
        if counted is None:
            counted = segments_per_file.get(file.get("name") or "", 1)
        whole_file = int(counted) == 1

        events.append(
            {
                "id": f"{entry_id}:{channel}:{file['start_id']}",
                "entry_id": entry_id,
                "nvr": nvr_name,
                "channel": channel,
                "camera": camera_name,
                "start": file["start"],
                "end": file["end"],
                "duration": duration,
                "triggers": file.get("triggers") or [],
                "size": file.get("size") or 0,
                # False when `size` describes the parent recording, not this row.
                "size_is_exact": whole_file,
                "streams": sorted(
                    streams,
                    key=lambda stream: (stream != STREAM_MAIN, stream != STREAM_SUB),
                ),
                "files": streams,
                "playable": playable,
                # How the camera records, measured per day. The player only trims a clip
                # to its detections on continuous footage: where the camera records on
                # events, the recorder already did that job and trimming twice would eat
                # into its own pre-record buffer.
                "continuous": continuous,
                # Offered in the quality menu; resolved on demand, never searched up front.
                "alternate_streams": [
                    name for name in (alternate_streams or []) if name not in streams
                ],
                "pre_roll": _pre_roll_for(camera, pre_roll_default, duration),
            }
        )

    events.sort(key=lambda event: event["start"], reverse=True)
    return events


def async_playback_path(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start_id: str,
    end_id: str,
) -> dict[str, Any]:
    """Build the (unsigned) proxy path that streams a recording.

    Reuses the Reolink integration's own authenticated video proxy rather than
    reimplementing it. The caller signs this path via Home Assistant's ``auth/sign_path``
    websocket command before handing it to a <video> element.

    Imports are deferred: reolink_aio is only installed once the Reolink integration is
    present, and this integration must still load without it.
    """
    from homeassistant.components.reolink.views import (
        async_generate_playback_proxy_url,
    )
    from reolink_aio.enums import VodRequestType

    host = async_get_host(hass, entry_id)
    api = host.api

    if not api.is_nvr:
        # Standalone cameras are filtered out of the NVR list, so this is unreachable
        # in practice; refuse clearly rather than guessing an RTMP/HLS path.
        raise NotImplementedError("Only NVR playback is supported by this panel")

    # Which request type a recorder accepts varies by model and firmware, and the
    # choice the Reolink media source makes is not universally right: on an RLN8-410
    # (fw v3.6.5) every recording has a synthetic, extension-less name, which sends
    # that logic down NvrDownload -- and the NVR answers HTTP 400 "Server disconnected"
    # for both NvrDownload and Playback, while Download returns a valid MP4.
    #
    # So rather than commit to one, return the candidates in the order most likely to
    # work and let the player fall through on error. That keeps this working on
    # hardware nobody here can test.
    candidates: list[tuple[VodRequestType, str]] = [
        (VodRequestType.DOWNLOAD, filename),
        # Identifies the recording by time range rather than by name.
        (VodRequestType.NVR_DOWNLOAD, f"{start_id}_{end_id}"),
        (VodRequestType.PLAYBACK, filename),
    ]

    resolved = [
        {
            "vod_type": vod_type.value,
            "path": async_generate_playback_proxy_url(
                entry_id, channel, name, stream, vod_type.value
            ),
        }
        for vod_type, name in candidates
    ]

    return {
        # First candidate is the one to try; `candidates` is the fallback chain.
        "path": resolved[0]["path"],
        "vod_type": resolved[0]["vod_type"],
        "mime": "video/mp4",
        "candidates": resolved,
    }
