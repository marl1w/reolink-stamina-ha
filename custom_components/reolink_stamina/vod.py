"""Recording search, event composition and playback URLs.

Talks to the device through the live reolink_aio object and turns its answers into plain
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
from .reolink_registry import async_get_host

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

    A day that has not begun yet cannot be measured at all, and must not be guessed at.
    Home Assistant's local date and the day being asked about routinely differ by one
    either side of midnight — the panel asks for a date, not for "today" — so `elapsed`
    comes out negative, and clamping that to a fraction of a second made any recording
    whatsoever read as full coverage. Every camera then reported continuous for hours
    around the date line, and the panel trims a continuous day rather than listing it.
    """
    if unlabelled > 0:
        return True
    start, end = _day_bounds(date)
    recorded = sum(item.get("duration") or 0.0 for item in files)
    day_end = min(dt_util.now().replace(tzinfo=None), end)
    elapsed = (day_end - start).total_seconds()
    if elapsed <= 0:
        return False
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


def _wall_clock(moment: dt.datetime) -> dt.datetime:
    """Return the wall clock a timestamp was reported as, with any assumed zone stripped."""
    return moment.replace(tzinfo=None)


# Only two conventions are being told apart and they differ by the whole UTC offset, so
# the slack here only has to absorb a recorder that rounds one of the two timestamps.
_CONVENTION_SLACK = dt.timedelta(seconds=2)


def stated_playback_time(file: Any) -> dt.datetime | None:
    """Return the recording's own start as the recorder stated it, or None if it did not.

    `PlaybackTime` is not a field every device answers a search with. Hubs in particular
    return StartTime, EndTime and a file name and nothing else, and reolink_aio reads the
    field unconditionally -- so the very first recording killed the whole camera-day with a
    KeyError whose entire message was the bare field name. The cache keeps a failed search's
    previous answer and reports the error beside it, which is why the panel read

        Could not reach the device, showing results from 1 min ago. 'PlaybackTime'

    -- a message about the network for a device that was answering perfectly well. The
    absence is a shape the library does not cover, not a fault, so it is read as a question
    with two answers rather than as an exception.

    The catch is wider than the absence that prompted it. A field that arrives unreadable
    is answered the same way, because the alternative is the failure this replaced: one
    unparseable row taking a whole camera-day down with it. So "the recorder did not state
    it" is shorthand throughout for "nothing usable came back", and `playback_derived`
    cannot tell the two apart -- it reports that StartTime stood in, not why it had to.
    """
    try:
        return file.playback_time
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _derived_playback_time(start: dt.datetime, *, playback_is_utc: bool) -> dt.datetime:
    """Return the recording's start as `PlaybackTime` would have stated it.

    Which zone that is is not ours to pick: it is whatever the recorder was measured to
    state the field in. A recorder keeping UTC -- the library's assumption, and the only
    answer available where every row abstained -- wants the instant converted. One keeping
    its own time wants the wall clock it would itself have sent, because that is what the
    endpoint will be asked for. Deriving in UTC regardless would put a device that omits
    the field on only some of its rows an entire offset away from itself on those rows,
    while the rows either side of them stayed right.

    A device reporting no offset at all is left as it is under either convention: its wall
    clock is the only time it has, and converting from an assumed zone would move the
    request.
    """
    if not playback_is_utc or start.tzinfo is None:
        return _wall_clock(start)
    return start.astimezone(dt.UTC)


def _file_name(file: Any, playback_time: dt.datetime) -> str:
    """Return what the recording is called, naming it by timestamp if the device does not.

    reolink_aio already falls back to a bare timestamp for a device that returns no file
    name -- but it builds that timestamp out of `PlaybackTime`, so on a device stating
    neither it raises the same KeyError one line further on. Given the timestamp instead of
    going back to the file for it, the fallback works on both.
    """
    try:
        return file.file_name
    except (AttributeError, KeyError, TypeError, ValueError):
        return playback_time.strftime("%Y%m%d%H%M%S")


def playback_time_is_utc(files: list[Any]) -> bool:
    """Measure whether the recorder states PlaybackTime in UTC or in its own time.

    reolink_aio reads `PlaybackTime` as UTC unconditionally. On the recorders this was
    written against that is correct, but it is not universal, and where it does not hold
    converting the timestamp again moves it by the whole UTC offset -- naming a moment
    hours before the recording exists. The recorder then answers 404 for every clip on
    every camera whatever the stream, which is what issue #1 turned out to be. Invisible
    on a recorder keeping UTC, and invisible in the log, because the request itself is
    perfectly well formed.

    So it is measured rather than assumed, from the search results themselves. Splitting
    rewrites StartTime and copies PlaybackTime, so among the rows sharing one PlaybackTime
    the earliest is the one that still begins where the recording does -- and for that row
    the two timestamps describe the same instant. Their difference as bare wall clocks is
    therefore zero if PlaybackTime is already in the recorder's own time, or exactly the
    UTC offset if it is in UTC.

    Rows matching neither convention are not evidence and do not vote: a recording that
    began before the day being searched can come back clipped to the window, and a clipped
    StartTime is not the file's start. A recorder keeping UTC cannot be told apart at all,
    since both conventions agree there -- and it does not matter, because they agree.

    A device that does not state the field at all has no convention to measure, and every
    row abstains. What is returned then is immaterial: `serialize_file` derives the
    timestamp from StartTime instead and never consults this.
    """
    earliest: dict[dt.datetime, tuple[dt.datetime, dt.timedelta]] = {}
    for file in files:
        stated = stated_playback_time(file)
        if stated is None:
            continue
        try:
            start = file.start_time
            playback = _wall_clock(stated)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        known = earliest.get(playback)
        if known is None or _wall_clock(start) < known[0]:
            earliest[playback] = (_wall_clock(start), start.utcoffset() or dt.timedelta(0))

    as_utc = as_local = 0
    for playback, (start, utc_offset) in earliest.items():
        if not utc_offset:
            continue
        gap = start - playback
        if abs(gap - utc_offset) <= _CONVENTION_SLACK:
            as_utc += 1
        elif abs(gap) <= _CONVENTION_SLACK:
            as_local += 1

    if as_local > as_utc:
        _LOGGER.debug(
            "Recorder states PlaybackTime in its own time, not UTC (%s recordings agree, "
            "%s disagree); using it unconverted",
            as_local,
            as_utc,
        )
        return False
    return True


def serialize_file(
    file: Any,
    *,
    playback_is_utc: bool = True,
    source_entry_id: str | None = None,
    source_channel: int | None = None,
) -> dict[str, Any]:
    """Turn a reolink_aio VOD_file into a JSON-safe dict.

    Splitting is why this is more involved than it looks. Recorders write long files -- half
    an hour on the hardware tested -- and `split_time` cuts them into rows by rewriting
    StartTime and EndTime only. `PlaybackTime` and the file name are copied from the parent
    unchanged, and those two are what the playback endpoint actually locates a recording by.
    Asking for a window at 08:50 therefore replays the file from 08:30 unless the position
    inside the file is stated separately.

    So both are recorded: the file's own start, which identifies the recording, and this
    window's offset into it, which is where playback should begin.

    `playback_is_utc` is which zone the recorder states PlaybackTime in, measured by
    `playback_time_is_utc` over a whole search rather than decided per row. It defaults to
    the library's own assumption, which is right far more often than not.

    A device that does not state PlaybackTime at all is served by StartTime, and safely:
    the field exists to survive splitting, and reolink_aio does not split the recordings of
    a device that is not an NVR -- hubs included, explicitly. Every row from such a device
    is therefore a whole recording whose StartTime *is* the file's start, so the anchor is
    exact and the offset into it is zero.

    Splitting is the one thing this cannot stand in for, and the bound is not a small one:
    a segment's start is not where any recording begins, so a split row derived this way
    would ask the endpoint to locate a file by a timestamp no file has, and get either
    nothing or the parent from its own beginning -- a whole split interval early, with an
    offset of zero to correct it by. That is not reachable from here: the same library rule
    that makes the field absent on a hub is the rule that stops a hub being split. It is
    written down because the two facts have to keep holding together, and only the second
    of them is enforced anywhere.
    """
    start = file.start_time
    end = file.end_time

    # PlaybackTime is the *file's* start and survives splitting, so it is the only reliable
    # anchor for where this window sits inside the recording. reolink_aio hands it over
    # labelled UTC; where that label is wrong it is the recorder's own time already and
    # converting it would move the request the whole UTC offset away from the recording.
    stated = stated_playback_time(file)
    derived = stated is None
    if stated is None:
        playback_time = _derived_playback_time(start, playback_is_utc=playback_is_utc)
        file_start = start
    else:
        playback_time = stated
        if playback_is_utc:
            file_start = playback_time.astimezone(start.tzinfo)
        else:
            file_start = playback_time.replace(tzinfo=start.tzinfo)
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
        # Which zone the recorder was measured to state PlaybackTime in, so a diagnostics
        # report says how `file_start_id` was arrived at rather than leaving it to be
        # inferred from the two timestamps.
        "playback_is_utc": playback_is_utc,
        # True when no usable PlaybackTime came back and StartTime stood in for it.
        # `playback_is_utc` says nothing on such a device -- nothing was measured -- so
        # without this a diagnostics report cannot tell a measured convention from an
        # absent field, which is the difference between a wrong timestamp and no timestamp.
        "playback_derived": derived,
        # Which device actually answered the search, so playback addresses the same one.
        # Normally the camera the panel is showing; different only for a camera whose
        # recordings live on a recorder whose copy of it is disabled in Home Assistant. It is
        # recorded per row rather than per day because this is what a playback URL is built
        # from, and a row outlives the search that produced it -- it is served from the cache
        # long after, and asking the wrong device then returns a well-formed nothing.
        "source_entry_id": source_entry_id,
        "source_channel": source_channel,
        # Seconds from the start of the recording to the start of this window.
        "offset": offset,
        "name": _file_name(file, playback_time),
        "size": file.size,
        "type": file.type,
        "triggers": trigger_names(file.triggers),
        # Filled in by `_async_classify`: what Home Assistant's sensors detected inside
        # this window, and how many times each fired. Empty until then, and empty for
        # good on a recorder-less install.
        "kinds": [],
        "counts": {},
        "duration": max(0.0, (end - start).total_seconds()),
    }


def _as_utc(value: str) -> dt.datetime | None:
    """Parse a timestamp to UTC, whatever offset it arrived with.

    A recording's times carry the recorder's offset; a detection's carry the recorder
    database's UTC. Anything naive is read as local time, which is what a device that
    reports no offset at all means by it.
    """
    parsed = dt_util.parse_datetime(value)
    return None if parsed is None else dt_util.as_utc(parsed)


async def _async_classify(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    start: dt.datetime,
    end: dt.datetime,
    files: list[dict[str, Any]],
) -> None:
    """Stamp each recording with what the detection sensors saw inside it.

    Counted by where a detection *started*, so a person walking across a segment boundary
    is counted once, against the segment they walked into rather than both.

    Best effort by design: no recorder, a purged history or a camera with no detection
    sensors all mean no kinds, and the rows then fall back to the recorder's own flags.

    Everything is compared as an absolute instant, and it has to be: the recorder answers
    in UTC while the device answers in its own offset, so the same moment arrives as
    18:06:38+00:00 from one and 20:06:30+02:00 from the other. Dropping the offsets to
    compare them as wall clocks means no detection ever lands inside any recording — off
    by exactly the local offset, everywhere except UTC.
    """
    from .detections import async_detections_in_window

    try:
        detections = await async_detections_in_window(
            hass, entry_id, channel, dt_util.as_utc(start), dt_util.as_utc(end)
        )
    except Exception:
        _LOGGER.debug("Could not classify %s channel %s from sensors", entry_id, channel)
        return
    if not detections:
        return

    moments = [(_as_utc(item["at"]), item["kind"]) for item in detections]
    for data in files:
        window_start = _as_utc(data["start"])
        window_end = _as_utc(data["end"])
        if window_start is None or window_end is None:
            continue
        counts: dict[str, int] = {}
        for at, kind in moments:
            if at is not None and window_start <= at < window_end:
                counts[kind] = counts.get(kind, 0) + 1
        if counts:
            data["kinds"] = sorted(counts)
            data["counts"] = counts


async def async_search_day(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    stream: str,
    date: dt.date,
    split_minutes: int,
    include_unlabelled: bool = False,
    classify: bool = True,
    *,
    source_entry_id: str | None = None,
    source_channel: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Search one camera's recordings for one day on one stream.

    Returns the recordings worth keeping, and how many unlabelled ones were discarded.

    `entry_id` and `channel` name the camera; `source_entry_id` and `source_channel` say
    which device to ask, and default to the same thing. They differ only for a camera set
    up twice -- directly, and as a channel on a recorder whose copy the user disabled --
    where the recordings are on the recorder while the detection sensors belong to the
    direct entry. The search then runs against the recorder and the classification against
    the camera, because each is asking the device that actually knows.

    Every row records which device answered, so playback addresses the same one. Nothing
    else moves: the camera's identity in the panel, its event ids and its journal key stay
    the direct entry's, which is why the detection and relevance features need no idea any
    of this happened.

    A recorder with 24/7 recording enabled reports continuous footage with no trigger
    flag at all, and it dominates the results — 192 unlabelled segments against 6 real
    detections in one day on the hardware this was tested on. Those are dropped here,
    the earliest point we control, so they are never serialised, never persisted to
    .storage, never sent over the websocket and never turned into rows.

    Note this cannot save the device any work: reolink_aio's own `trigger=` argument runs
    the same single search and then returns one bucket from it, so filtering per trigger
    would mean more round trips, not fewer. What it saves is everything after that.
    """
    search_entry_id = source_entry_id if source_entry_id is not None else entry_id
    search_channel = source_channel if source_channel is not None else channel
    host = async_get_host(hass, search_entry_id)
    start, end = _day_bounds(date)

    split_time: dt.timedelta | None = None
    if split_minutes:
        clamped = min(max(split_minutes, _MIN_SPLIT_MINUTES), _MAX_SPLIT_MINUTES)
        split_time = dt.timedelta(minutes=clamped)

    _LOGGER.debug(
        "Searching %s channel %s %s stream on %s%s",
        search_entry_id,
        search_channel,
        stream,
        date.isoformat(),
        ""
        if (search_entry_id, search_channel) == (entry_id, channel)
        else f" for {entry_id} channel {channel}",
    )
    _, vod_files = await host.api.request_vod_files(
        search_channel,
        start,
        end,
        stream=stream,
        split_time=split_time,
    )

    # Measured over the whole result set, before anything is serialised: telling the two
    # PlaybackTime conventions apart needs the rows that share a recording, and a single
    # row cannot say which convention produced it.
    playback_is_utc = playback_time_is_utc(vod_files)
    serialised = [
        serialize_file(
            file,
            playback_is_utc=playback_is_utc,
            source_entry_id=search_entry_id,
            source_channel=search_channel,
        )
        for file in vod_files
    ]

    # Said once per search rather than per recording: a device that omits PlaybackTime omits
    # it from every row, and the interesting number is how many rows that was.
    derived = sum(1 for data in serialised if data["playback_derived"])
    if derived:
        _LOGGER.debug(
            "%s channel %s states no PlaybackTime (%s of %s recordings); "
            "using StartTime as the recording's own start",
            entry_id,
            channel,
            derived,
            len(serialised),
        )

    # What Home Assistant saw, folded in before anything is judged or discarded.
    #
    # The recorder's own trigger flags are not a reliable account of what happened: a
    # camera with no on-board AI is classified by the recorder, and it writes those
    # recordings tagged as nothing at all — six minutes of footage its own detector was
    # calling a person throughout. So the detection sensors decide what a row is, and the
    # recorder's flags are the fallback for when the sensors cannot answer, which happens
    # whenever Home Assistant was down or its history has been purged.
    #
    # One history query per camera-day, against the search that just cost a round trip to
    # the recorder. It is stamped onto the rows here so it reaches the cache: the panel
    # then reads it straight out of storage instead of asking again per row.
    if classify:
        await _async_classify(hass, entry_id, channel, start, end, serialised)

    # Whether an unlabelled recording is filler or the event itself depends entirely on
    # how the camera records, and the recorder does not say which. Dropping unlabelled
    # recordings from an event-recording camera would hide every event it has, which is
    # exactly what happened to a camera whose recorder reports no event type at all.
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
        # Filler is a segment nothing was detected in and the recorder tagged as nothing.
        # A detection is enough to keep it, whatever the recorder thinks — that is the
        # whole point of asking Home Assistant first.
        if not data["triggers"] and not data["kinds"] and continuous and not include_unlabelled:
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

    Uses the device's own per-month bitmap, which is one cheap call instead of searching
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
    an estimate and says so, because wired cameras on a recorder generally do not expose
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


def _kinds_for(file: dict[str, Any]) -> list[str]:
    """Return how a recording should be presented.

    Sensors first, recorder second. `timer` is carried over from the recorder even when
    the sensors have an opinion, because it is the one thing they cannot report.
    """
    detected = list(file.get("kinds") or [])
    if not detected:
        return list(file.get("triggers") or [])
    if "timer" in (file.get("triggers") or []):
        detected.append("timer")
    return detected


def build_events(
    *,
    entry_id: str,
    device_name: str,
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
                # Which device to ask for these bytes, where that is not the camera the row
                # is filed under. Carried per resolution because each is searched separately.
                "source_entry_id": file.get("source_entry_id"),
                "source_channel": file.get("source_channel"),
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
                    "source_entry_id": best.get("source_entry_id"),
                    "source_channel": best.get("source_channel"),
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
                # Which device the recordings actually came from, where that is not this
                # camera. The row says so, because footage arriving from somewhere other
                # than the camera named on it is not something the timestamps would reveal.
                "source_entry_id": file.get("source_entry_id"),
                "source_channel": file.get("source_channel"),
                "device": device_name,
                "channel": channel,
                "camera": camera_name,
                "start": file["start"],
                "end": file["end"],
                "duration": duration,
                "triggers": file.get("triggers") or [],
                # What the row *is*, and what the filters match on: the sensors' verdict
                # where there is one, the recorder's own flags where there is not.
                # `timer` survives either way — nothing in Home Assistant reports that a
                # recording was scheduled, so dropping it would empty that filter.
                "kinds": _kinds_for(file),
                # Per kind, how many times it fired. The recorder says a segment carried a
                # person, never that it carried three.
                "counts": file.get("counts") or {},
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
