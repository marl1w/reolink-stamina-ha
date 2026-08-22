"""Tests for search, trigger decomposition, availability matching and pre-roll."""

from __future__ import annotations

import datetime as dt

from homeassistant.util import dt as dt_util
from reolink_aio.typings import VOD_trigger

from custom_components.reolink_stamina.vod import (
    async_search_calendar,
    async_search_day,
    build_events,
    is_continuous_day,
    playback_time_is_utc,
    serialize_file,
    trigger_names,
)

from .conftest import FakeApi, FakeHost, FakeSearchStatus, FakeVodFile

TZ = dt.timezone(dt.timedelta(hours=2))


def _file(hour: int, minute: int, seconds: int = 30, **kwargs) -> FakeVodFile:
    start = dt.datetime(2026, 8, 3, hour, minute, 0, tzinfo=TZ)
    return FakeVodFile(start, start + dt.timedelta(seconds=seconds), **kwargs)


# --------------------------------------------------------------------- triggers


def test_trigger_names_decomposes_a_combined_flag() -> None:
    """A recording can carry several triggers at once; all must be reported."""
    triggers = VOD_trigger.PERSON | VOD_trigger.MOTION
    assert set(trigger_names(triggers)) == {"person", "motion"}


def test_trigger_names_handles_none() -> None:
    """An unclassified recording reports no triggers rather than 'NONE'."""
    assert trigger_names(VOD_trigger.NONE) == []


def test_trigger_names_covers_every_member() -> None:
    """Every VOD_trigger the library defines must serialise to a usable name.

    Guards against a new upstream trigger silently arriving as an empty label.
    """
    for member in VOD_trigger:
        if member is VOD_trigger.NONE:
            continue
        assert trigger_names(member) == [member.name.lower()]


def test_serialize_file_is_json_safe() -> None:
    """Serialised files carry everything the panel and playback need."""
    file = _file(14, 2, triggers=VOD_trigger.VEHICLE)
    data = serialize_file(file)
    assert data["triggers"] == ["vehicle"]
    assert data["duration"] == 30.0
    assert data["start"].startswith("2026-08-03T14:02:00")
    assert data["start_id"] == "20260803140200"
    assert data["size"] == 1024


# ----------------------------------------------------------------------- search


async def test_search_day_passes_split_time(hass, patch_host) -> None:
    """split_time is what keeps triggers meaningful on continuous recordings."""
    api = patch_host.api
    await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 5)

    call = api.search_calls[-1]
    assert call["split_time"] == dt.timedelta(minutes=5)
    assert call["stream"] == "sub"
    assert call["start"] == dt.datetime(2026, 8, 3, 0, 0, 0)
    assert call["end"] == dt.datetime(2026, 8, 3, 23, 59, 59)


async def test_search_day_zero_split_means_whole_files(hass, patch_host) -> None:
    """0 minutes means 'do not split', which the library expects as None."""
    await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 0)
    assert patch_host.api.search_calls[-1]["split_time"] is None


async def test_search_day_clamps_absurd_split(hass, patch_host) -> None:
    """A nonsense option must not be forwarded to the device verbatim."""
    await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 9999)
    assert patch_host.api.search_calls[-1]["split_time"] == dt.timedelta(minutes=60)


async def test_search_day_returns_sorted_files(hass) -> None:
    """Rows are built from this list, so ordering must be deterministic."""
    api = FakeApi(
        files={
            "sub": [
                _file(15, 0, triggers=VOD_trigger.PERSON),
                _file(14, 0, triggers=VOD_trigger.PERSON),
                _file(16, 0, triggers=VOD_trigger.PERSON),
            ]
        }
    )
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, _ = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 5)

    assert [item["start"][11:16] for item in files] == ["14:00", "15:00", "16:00"]


async def test_search_calendar_returns_only_the_asked_month(hass) -> None:
    """The device answers per month; a stray month must not leak into the grid."""
    api = FakeApi(
        statuses=[
            FakeSearchStatus(2026, 8, (1, 3, 17)),
            FakeSearchStatus(2026, 7, (9,)),
        ]
    )
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        days = await async_search_calendar(hass, "entry", 0, 2026, 8)

    assert days == [1, 3, 17]


async def test_search_calendar_uses_status_only(hass, patch_host) -> None:
    """The calendar must use the cheap bitmap call, not a full day-by-day search."""
    await async_search_calendar(hass, "entry", 0, 2026, 8)
    assert patch_host.api.search_calls[-1]["status_only"] is True


# ------------------------------------------------------------------ event rows


def _events(primary, other=None, camera=None, pre_roll=5, continuous=False):
    return build_events(
        entry_id="entry",
        device_name="Test NVR",
        channel=0,
        camera=camera or {"name": "Driveway"},
        primary_stream="sub",
        primary_files=[serialize_file(file) for file in primary],
        other_files={
            stream: [serialize_file(file) for file in files]
            for stream, files in (other or {}).items()
        },
        pre_roll_default=pre_roll,
        continuous=continuous,
    )


def test_events_are_newest_first() -> None:
    """The timeline reads newest first."""
    events = _events([_file(14, 0), _file(16, 0), _file(15, 0)])
    assert [event["start"][11:16] for event in events] == ["16:00", "15:00", "14:00"]


def test_event_carries_camera_and_device_names() -> None:
    """Rows must be attributable when several devices are merged into one list."""
    event = _events([_file(14, 0)])[0]
    assert event["camera"] == "Driveway"
    assert event["device"] == "Test NVR"
    assert event["channel"] == 0


def test_matching_clip_in_other_stream_marks_both_available() -> None:
    """An overlapping file in the other resolution is the availability badge."""
    event = _events([_file(14, 0)], {"main": [_file(14, 0)]})[0]
    assert event["streams"] == ["main", "sub"]
    assert set(event["files"]) == {"main", "sub"}


def test_non_overlapping_file_is_not_treated_as_the_same_clip() -> None:
    """A neighbouring segment must not be mistaken for this event's clip."""
    event = _events([_file(14, 0)], {"main": [_file(20, 0)]})[0]
    assert event["streams"] == ["sub"]


def test_missing_other_stream_reports_only_what_exists() -> None:
    """NVRs commonly record one resolution only; that must be shown honestly."""
    event = _events([_file(14, 0)], {"main": []})[0]
    assert event["streams"] == ["sub"]


def test_zero_byte_recording_is_not_playable() -> None:
    """A clip reporting no bytes should not open a player that cannot work."""
    event = _events([_file(14, 0, size=0)])[0]
    assert event["playable"] is False


def test_event_id_is_stable() -> None:
    """Row identity drives keyed reconciliation, so it must not drift."""
    first = _events([_file(14, 0)])[0]
    second = _events([_file(14, 0)])[0]
    assert first["id"] == second["id"] == "entry:0:20260803140000"


# -------------------------------------------------------------------- pre-roll


def test_pre_roll_is_estimated_when_camera_does_not_report_it() -> None:
    """Wired NVR cameras usually cannot report pre-record; say so, don't invent it."""
    event = _events([_file(14, 0)], camera={"name": "Drive"}, pre_roll=7)
    assert event[0]["pre_roll"] == {"seconds": 7.0, "exact": False}


def test_pre_roll_is_exact_when_the_camera_reports_it() -> None:
    """A camera that exposes its pre-record time gets a precise marker."""
    camera = {
        "name": "Gate",
        "pre_record": {"supported": True, "enabled": True, "seconds": 4},
    }
    event = _events([_file(14, 0)], camera=camera)
    assert event[0]["pre_roll"] == {"seconds": 4.0, "exact": True}


def test_disabled_pre_record_falls_back_to_the_estimate() -> None:
    """Reporting the setting is not the same as having it switched on."""
    camera = {
        "name": "Gate",
        "pre_record": {"supported": True, "enabled": False, "seconds": 4},
    }
    event = _events([_file(14, 0)], camera=camera, pre_roll=9)
    assert event[0]["pre_roll"] == {"seconds": 9.0, "exact": False}


def test_pre_roll_never_exceeds_the_clip() -> None:
    """A marker past the end of the clip would be nonsense."""
    event = _events([_file(14, 0, seconds=3)], pre_roll=30)
    assert event[0]["pre_roll"]["seconds"] == 3.0


def test_unclassified_recording_keeps_empty_triggers() -> None:
    """The panel shows these as plain recordings rather than hiding them."""
    event = _events([_file(14, 0)])[0]
    assert event["triggers"] == []


# ------------------------------------------------- discarding continuous recording


async def test_unlabelled_recordings_are_discarded_on_a_24_7_camera(hass) -> None:
    """Continuous footage must not be stored, sent, or turned into rows.

    A camera recording around the clock reports it with no trigger flag at all, and it
    outnumbers real detections by roughly 30:1, so it is dropped at the source.
    """
    # Enough recorded time to look like continuous coverage of a whole past day.
    api = FakeApi(
        files={
            "sub": [
                _file(1, 0, seconds=11 * 3600),
                _file(13, 0, seconds=10 * 3600),
                _file(9, 0, triggers=VOD_trigger.PERSON),
                _file(9, 15, triggers=VOD_trigger.ANIMAL),
            ]
        }
    )
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, unlabelled = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 1), 5)

    assert unlabelled == 2
    assert [f["triggers"] for f in files] == [["person"], ["animal"]]


async def test_unlabelled_recordings_are_kept_on_an_event_camera(hass) -> None:
    """An event-recording camera's unlabelled recordings *are* its events.

    Some recorders report no event type at all for such a camera — measured on an
    RLN8-410, where a camera with two recordings in an hour had eventType absent. Dropping
    them as filler hid every event that camera had.
    """
    api = FakeApi(files={"sub": [_file(15, 25, seconds=275), _file(15, 30, seconds=189)]})
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, unlabelled = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 1), 5)

    assert unlabelled == 0
    assert len(files) == 2


async def test_unlabelled_recordings_can_be_included(hass) -> None:
    """The option exists for recorders that report no trigger types at all."""
    api = FakeApi(files={"sub": [_file(9, 0), _file(9, 5)]})
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, unlabelled = await async_search_day(
            hass, "entry", 0, "sub", dt.date(2026, 8, 3), 5, include_unlabelled=True
        )

    assert unlabelled == 0
    assert len(files) == 2


# ------------------------------------------------------- continuous vs event mode


def test_a_day_covered_by_recordings_is_continuous() -> None:
    """24/7 footage covers the day; that is the whole signal."""
    files = [serialize_file(_file(0, 0, seconds=20 * 3600))]
    assert is_continuous_day(files, dt.date(2026, 8, 1)) is True


def test_a_day_with_a_few_clips_is_event_recording() -> None:
    """A camera recording on events covers almost none of its day."""
    files = [serialize_file(_file(9, 0, seconds=30)), serialize_file(_file(15, 0, seconds=45))]
    assert is_continuous_day(files, dt.date(2026, 8, 1)) is False


def test_a_day_that_has_not_begun_is_not_continuous() -> None:
    """Nothing has elapsed to measure coverage against, so nothing may be concluded.

    Home Assistant's local date and the day being asked about differ by one either side of
    midnight, which used to clamp the elapsed day to a fraction of a second and make a
    single clip read as full coverage — every camera continuous, for hours a day.
    """
    tomorrow = dt_util.now().date() + dt.timedelta(days=1)
    files = [serialize_file(_file(9, 0, seconds=30))]
    assert is_continuous_day(files, tomorrow) is False


def test_discarded_recordings_prove_the_day_was_continuous() -> None:
    """The kept files no longer cover the day, so coverage alone would say the opposite.

    Unlabelled recordings are only ever dropped from a continuous camera, so having
    dropped any is proof of the mode — and this is the case the player must not get wrong,
    because trimming an event camera's clip would eat its pre-record buffer.
    """
    files = [serialize_file(_file(9, 0, seconds=30))]
    assert is_continuous_day(files, dt.date(2026, 8, 1), unlabelled=180) is True


def test_events_carry_how_the_camera_records() -> None:
    """The player trims clips to their detections on 24/7 footage only."""
    events = _events([_file(9, 0, triggers=VOD_trigger.PERSON)], continuous=True)
    assert events[0]["continuous"] is True

    events = _events([_file(9, 0, triggers=VOD_trigger.PERSON)])
    assert events[0]["continuous"] is False


def test_size_is_marked_inexact_for_split_segments() -> None:
    """A slice of a long recording must not claim the whole file's size.

    The device reports one size per recording, so segments of 24/7 footage would each
    claim hundreds of megabytes while an event-triggered clip of the same length claims
    a few — which reads as though they were wildly different recordings.
    """
    shared = "0-8-0-01260703070001-00000"
    events = _events(
        [
            _file(9, 0, triggers=VOD_trigger.PERSON, name=shared),
            _file(9, 5, triggers=VOD_trigger.PERSON, name=shared),
        ]
    )
    assert [e["size_is_exact"] for e in events] == [False, False]


def test_size_is_exact_for_a_whole_recording() -> None:
    """An event-triggered clip is the whole file, so its size is real."""
    events = _events([_file(9, 0, triggers=VOD_trigger.PERSON, name="one-off.mp4")])
    assert events[0]["size_is_exact"] is True


def test_a_counted_segment_beats_counting_the_survivors() -> None:
    """The search's own count wins, because only it saw the recordings it discarded."""
    serialised = serialize_file(_file(9, 0, triggers=VOD_trigger.PERSON, name="shared"))
    serialised["segments"] = 12

    events = build_events(
        entry_id="entry",
        device_name="Test NVR",
        channel=0,
        camera={"name": "Driveway"},
        primary_stream="sub",
        primary_files=[serialised],
        other_files={},
        pre_roll_default=5,
    )

    assert events[0]["size_is_exact"] is False, "one surviving slice of twelve is not the file"


async def test_a_surviving_slice_of_continuous_footage_does_not_claim_the_whole_file(
    hass,
) -> None:
    """The bug this exists for, measured on a real recorder.

    On a 24/7 camera all but a handful of a recording's segments are unlabelled and dropped, so
    counting the survivors saw one segment and called it the whole file. A row then
    reported 125 MB for 300 seconds of *low-resolution* footage — about 3.3 Mbit/s, which no sub
    stream produces — because the size belonged to the parent recording, not the row.
    """
    shared = "0-8-0-01260703070001-00000"
    api = FakeApi(
        files={
            "sub": [
                # A continuous day, all one recording, of which one segment is tagged.
                _file(1, 0, seconds=11 * 3600, name=shared),
                _file(13, 0, seconds=10 * 3600, name=shared),
                _file(9, 0, triggers=VOD_trigger.PERSON, name=shared),
            ]
        }
    )
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, unlabelled = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 1), 5)

    assert unlabelled == 2
    assert len(files) == 1
    assert files[0]["segments"] == 3, "counted before anything was discarded"

    events = build_events(
        entry_id="entry",
        device_name="Test NVR",
        channel=0,
        camera={"name": "Driveway"},
        primary_stream="sub",
        primary_files=files,
        other_files={},
        pre_roll_default=5,
        continuous=True,
    )
    assert events[0]["size_is_exact"] is False


def _reolink_time(moment: dt.datetime) -> dict[str, int]:
    """Build a time in the shape the recorder uses."""
    return {
        "year": moment.year,
        "mon": moment.month,
        "day": moment.day,
        "hour": moment.hour,
        "min": moment.minute,
        "sec": moment.second,
    }


class _FakeVod:
    """A VOD_file standing in for the real one, sharing its time semantics.

    Mirrors the property that matters: after splitting, StartTime is the window's while
    PlaybackTime remains the parent recording's.
    """

    def __init__(self, data: dict, tzinfo: dt.tzinfo) -> None:
        self.data = data
        self.tzinfo = tzinfo

    def _at(self, key: str, tz: dt.tzinfo) -> dt.datetime:
        raw = self.data[key]
        return dt.datetime(
            raw["year"], raw["mon"], raw["day"], raw["hour"], raw["min"], raw["sec"], tzinfo=tz
        )

    @property
    def start_time(self) -> dt.datetime:
        return self._at("StartTime", self.tzinfo)

    @property
    def end_time(self) -> dt.datetime:
        return self._at("EndTime", self.tzinfo)

    @property
    def playback_time(self) -> dt.datetime:
        return self._at("PlaybackTime", dt.UTC)

    @property
    def start_time_id(self) -> str:
        return self.start_time.strftime("%Y%m%d%H%M%S")

    @property
    def end_time_id(self) -> str:
        return self.end_time.strftime("%Y%m%d%H%M%S")

    @property
    def file_name(self) -> str:
        return self.data["name"]

    @property
    def size(self) -> int:
        return self.data["size"]

    @property
    def type(self) -> str:
        return self.data["type"]

    @property
    def triggers(self):
        return VOD_trigger.NONE


def test_a_split_window_records_its_offset_into_the_recording() -> None:
    """A row inside a long recording must know where in it that row begins.

    Recorders write long files — half an hour on the hardware tested — and reolink_aio's
    splitter rewrites StartTime and EndTime only, copying PlaybackTime and the file name
    from the parent. Those two are what the playback endpoint locates a recording by, so
    without a separate offset every row of a 30 minute file replays it from the start: an
    08:50 event played 08:30.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    # The parent recording runs 08:30 to 09:00; this row is the 08:50 window of it.
    data = {
        "StartTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 50, tzinfo=tz)),
        "EndTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 55, tzinfo=tz)),
        "PlaybackTime": _reolink_time(dt.datetime(2026, 8, 4, 6, 30, tzinfo=dt.UTC)),
        "name": "1-4-0-01260704063000-00000",
        "size": 1024,
        "type": "sub",
    }
    result = serialize_file(_FakeVod(data, tz))

    # Twenty minutes into the recording.
    assert result["offset"] == 1200.0
    # Addresses the recording, not the window.
    assert result["file_start_id"] == "20260804083000"
    assert result["playback_id"] == "20260804063000"
    # The window keeps its own identity for ids and cross-resolution matching.
    assert result["start_id"] == "20260804085000"


def test_the_first_window_has_no_offset() -> None:
    """A row that begins where the recording does needs no seek."""
    tz = dt.timezone(dt.timedelta(hours=2))
    data = {
        "StartTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz)),
        "EndTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz)),
        "PlaybackTime": _reolink_time(dt.datetime(2026, 8, 4, 6, 30, tzinfo=dt.UTC)),
        "name": "1-4-0-01260704063000-00000",
        "size": 1024,
        "type": "sub",
    }
    assert serialize_file(_FakeVod(data, tz))["offset"] == 0.0


# ----------------------------------------- which zone the recorder states PlaybackTime in


# The recorder's own time, whatever reolink_aio labels the timestamp as.
_EASTERN = dt.timezone(dt.timedelta(hours=-4))


def _rows(
    tz: dt.tzinfo, playback: dt.datetime, *windows: tuple[dt.datetime, dt.datetime]
) -> list[_FakeVod]:
    """Rows of one recording: their own StartTime, the recording's PlaybackTime copied.

    `playback` is given as the wall clock the recorder puts in the field, which is all a
    recorder sends -- reolink_aio is what labels it UTC.
    """
    return [
        _FakeVod(
            {
                "StartTime": _reolink_time(start),
                "EndTime": _reolink_time(end),
                "PlaybackTime": _reolink_time(playback),
                "name": "20260806162829",
                "size": 1024,
                "type": "sub",
            },
            tz,
        )
        for start, end in windows
    ]


def test_a_recorder_stating_playback_time_in_utc_is_measured_as_such() -> None:
    """The recorders this was written against, and the library's own assumption."""
    tz = dt.timezone(dt.timedelta(hours=2))
    files = _rows(
        tz,
        dt.datetime(2026, 8, 4, 6, 30),
        (dt.datetime(2026, 8, 4, 8, 30), dt.datetime(2026, 8, 4, 8, 35)),
        (dt.datetime(2026, 8, 4, 8, 35), dt.datetime(2026, 8, 4, 8, 40)),
    )
    assert playback_time_is_utc(files) is True


def test_a_recorder_stating_playback_time_in_its_own_time_is_measured_as_such() -> None:
    """Issue #1: an RLN12W states it locally, and converting it again 404s every clip.

    The first row of a recording begins where the recording does, so its StartTime and the
    PlaybackTime it carries are the same instant. Identical wall clocks therefore mean no
    conversion is wanted -- reading them four hours apart is the bug.
    """
    files = _rows(
        _EASTERN,
        dt.datetime(2026, 8, 6, 16, 28, 29),
        (dt.datetime(2026, 8, 6, 16, 28, 29), dt.datetime(2026, 8, 6, 16, 33, 29)),
        (dt.datetime(2026, 8, 6, 16, 33, 29), dt.datetime(2026, 8, 6, 16, 38, 29)),
    )
    assert playback_time_is_utc(files) is False


def test_a_locally_stated_playback_time_is_not_converted_again() -> None:
    """What the recorder is asked for: the recording's own start, and the seek into it."""
    files = _rows(
        _EASTERN,
        dt.datetime(2026, 8, 6, 16, 28, 29),
        (dt.datetime(2026, 8, 6, 16, 48, 29), dt.datetime(2026, 8, 6, 16, 53, 29)),
    )
    data = serialize_file(files[0], playback_is_utc=False)

    # Addressed at the recording's real start, not four hours before it.
    assert data["file_start_id"] == "20260806162829"
    assert data["playback_id"] == "20260806162829"
    # Twenty minutes in, which the conversion used to swallow into the clamp.
    assert data["offset"] == 1200.0
    assert data["playback_is_utc"] is False


def test_a_recording_clipped_to_the_searched_day_does_not_decide_it() -> None:
    """A StartTime the search window truncated is not the file's start, so it cannot vote.

    Searching one day can return a recording that began the evening before, and matching
    neither convention has to mean 'no evidence' rather than tipping the measurement.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    files = _rows(
        tz,
        dt.datetime(2026, 8, 3, 23, 50),
        (dt.datetime(2026, 8, 4, 0, 0), dt.datetime(2026, 8, 4, 0, 5)),
    ) + _rows(
        tz,
        dt.datetime(2026, 8, 4, 6, 30),
        (dt.datetime(2026, 8, 4, 8, 30), dt.datetime(2026, 8, 4, 8, 35)),
    )
    assert playback_time_is_utc(files) is True


def test_a_recorder_keeping_utc_falls_back_to_the_library_assumption() -> None:
    """With no offset the two conventions are the same answer, so neither can be measured."""
    files = _rows(
        dt.UTC,
        dt.datetime(2026, 8, 4, 8, 30),
        (dt.datetime(2026, 8, 4, 8, 30), dt.datetime(2026, 8, 4, 8, 35)),
    )
    assert playback_time_is_utc(files) is True


def test_nothing_to_measure_falls_back_to_the_library_assumption() -> None:
    """An empty day, and a recorder reporting no time settings at all."""
    assert playback_time_is_utc([]) is True
    assert playback_time_is_utc([object()]) is True


async def test_search_day_measures_the_convention_once_for_the_whole_result(hass) -> None:
    """The measurement needs the rows that share a recording, so it cannot be per row.

    End to end: a locally-stating recorder must come out of the search addressed at the
    recording's real start, on every row including the split ones.
    """
    from unittest.mock import patch as mock_patch

    files = _rows(
        _EASTERN,
        dt.datetime(2026, 8, 6, 16, 28, 29),
        (dt.datetime(2026, 8, 6, 16, 28, 29), dt.datetime(2026, 8, 6, 16, 33, 29)),
        (dt.datetime(2026, 8, 6, 16, 33, 29), dt.datetime(2026, 8, 6, 16, 38, 29)),
    )
    api = FakeApi(files={"sub": files})

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        found, _ = await async_search_day(
            hass, "entry", 0, "sub", dt.date(2026, 8, 6), 5, include_unlabelled=True
        )

    assert [row["file_start_id"] for row in found] == ["20260806162829"] * 2
    assert [row["offset"] for row in found] == [0.0, 300.0]


# ----------------------------------------------- what a row is: sensors before the NVR


async def test_the_sensors_classify_a_recording_the_nvr_left_untagged(hass) -> None:
    """The B800 case, and the reason the merge lives here rather than in the panel.

    A camera with no on-board AI is classified by the NVR, and the NVR writes those
    recordings tagged as nothing at all — six minutes of footage its own detector was
    calling a person throughout. Home Assistant's sensors know; the recorder does not.
    """
    api = FakeApi(files={"sub": [_file(20, 6, seconds=370)]})
    runs = [
        {"at": "2026-08-03T18:06:38+00:00", "kind": "person"},
        {"at": "2026-08-03T18:07:30+00:00", "kind": "person"},
        {"at": "2026-08-03T18:11:02+00:00", "kind": "animal"},
        # After the recording ends: belongs to whatever came next, not to this row.
        {"at": "2026-08-03T18:59:00+00:00", "kind": "person"},
    ]
    from unittest.mock import patch as mock_patch

    with (
        mock_patch(
            "custom_components.reolink_stamina.vod.async_get_host", return_value=FakeHost(api)
        ),
        mock_patch(
            "custom_components.reolink_stamina.detections.async_detections_in_window",
            return_value=runs,
        ),
    ):
        files, _unlabelled = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 0)

    assert len(files) == 1
    assert files[0]["triggers"] == [], "the recorder tagged it as nothing"
    assert files[0]["kinds"] == ["animal", "person"]
    assert files[0]["counts"] == {"person": 2, "animal": 1}


async def test_a_detection_saves_a_segment_the_nvr_would_have_had_discarded(hass) -> None:
    """Sensors decide what is filler, not the recorder's silence.

    Continuous footage is still dropped wholesale — it has to be, it is 30:1 — but a
    segment Home Assistant saw someone in is not filler, whatever the recorder says.
    """
    api = FakeApi(
        files={
            "sub": [
                _file(1, 0, seconds=7 * 3600),
                _file(13, 0, seconds=10 * 3600),
                _file(9, 0, seconds=300),
            ]
        }
    )
    from unittest.mock import patch as mock_patch

    with (
        mock_patch(
            "custom_components.reolink_stamina.vod.async_get_host", return_value=FakeHost(api)
        ),
        mock_patch(
            "custom_components.reolink_stamina.detections.async_detections_in_window",
            return_value=[{"at": "2026-08-03T07:02:00+00:00", "kind": "person"}],
        ),
    ):
        files, unlabelled = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 0)

    assert unlabelled == 2, "the two long filler recordings still go"
    assert len(files) == 1
    assert files[0]["kinds"] == ["person"]


async def test_classification_is_skipped_when_the_caller_does_not_want_it(hass) -> None:
    """Cloud sync searches to locate bytes for a window the sensors already decided.

    Twelve stability attempts per clip, two days each: classifying there would be a
    history query per attempt for an answer the syncer is holding.
    """
    api = FakeApi(files={"sub": [_file(9, 0, triggers=VOD_trigger.PERSON)]})
    from unittest.mock import patch as mock_patch

    with (
        mock_patch(
            "custom_components.reolink_stamina.vod.async_get_host", return_value=FakeHost(api)
        ),
        mock_patch(
            "custom_components.reolink_stamina.detections.async_detections_in_window"
        ) as query,
    ):
        await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 0, classify=False)

    assert query.call_count == 0


def test_an_event_prefers_the_sensors_and_falls_back_to_the_recorder() -> None:
    """Both directions of the rule, including what happens when the recorder is all we have."""
    detected = serialize_file(_file(9, 0, triggers=VOD_trigger.MOTION))
    detected["kinds"] = ["person"]
    detected["counts"] = {"person": 3}
    recorder_only = serialize_file(_file(10, 0, triggers=VOD_trigger.VEHICLE))

    events = build_events(
        entry_id="entry",
        device_name="Test NVR",
        channel=0,
        camera={"name": "Driveway"},
        primary_stream="sub",
        primary_files=[detected, recorder_only],
        other_files={},
        pre_roll_default=5,
    )

    by_start = {event["start"][11:16]: event for event in events}
    assert by_start["09:00"]["kinds"] == ["person"], "the sensors outrank the recorder's 'motion'"
    assert by_start["09:00"]["counts"] == {"person": 3}
    assert by_start["10:00"]["kinds"] == ["vehicle"], "no detections, so the recorder decides"


def test_a_scheduled_recording_stays_scheduled_even_once_someone_walks_into_it() -> None:
    """Nothing in Home Assistant reports "this was a scheduled recording".

    So `timer` is the one flag carried over from the recorder even when the sensors have
    an opinion — dropping it would empty the Scheduled filter.
    """
    file = serialize_file(_file(9, 0, triggers=VOD_trigger.TIMER))
    file["kinds"] = ["person"]

    events = build_events(
        entry_id="entry",
        device_name="Test NVR",
        channel=0,
        camera={"name": "Driveway"},
        primary_stream="sub",
        primary_files=[file],
        other_files={},
        pre_roll_default=5,
    )

    assert sorted(events[0]["kinds"]) == ["person", "timer"]


# --------------------------------------------- a device that states no PlaybackTime at all


def _hub_file(start: dt.datetime, end: dt.datetime, tz: dt.tzinfo, **extra) -> _FakeVod:
    """Return a hub's answer to a search: StartTime, EndTime, a name, nothing else.

    `_FakeVod` reads `data["PlaybackTime"]` just as the real VOD_file does, so leaving the
    key out reproduces the failure exactly: KeyError from the property, carrying the bare
    field name that the panel ended up displaying as its whole explanation.
    """
    data = {
        "StartTime": _reolink_time(start),
        "EndTime": _reolink_time(end),
        "type": "sub",
        "size": 1024,
        **extra,
    }
    return _FakeVod(data, tz)


def test_a_recording_without_playback_time_is_serialised_rather_than_raising() -> None:
    """A hub states no PlaybackTime, and the search must survive it.

    reolink_aio reads the field unconditionally, so the first recording killed the whole
    camera-day with a KeyError whose entire message was the field name — which the cache
    reported beside stale data as "Could not reach the device ... 'PlaybackTime'", blaming
    the network for a device that was answering fine.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    file = _hub_file(
        dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz),
        dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz),
        tz,
        name="1-4-0-01260704063000-00000",
    )

    result = serialize_file(file)

    # The recording is addressed by its own start, in the recorder's time...
    assert result["file_start_id"] == "20260804083000"
    # ...and by the same instant in UTC, which is what the endpoint wants.
    assert result["playback_id"] == "20260804063000"
    # Nothing was split, so this row *is* the recording and there is nowhere to seek to.
    assert result["offset"] == 0.0
    assert result["playback_derived"] is True
    assert result["name"] == "1-4-0-01260704063000-00000"


def test_a_stated_playback_time_is_not_reported_as_derived() -> None:
    """The flag has to distinguish the two, or diagnostics cannot tell them apart."""
    tz = dt.timezone(dt.timedelta(hours=2))
    data = {
        "StartTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz)),
        "EndTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz)),
        "PlaybackTime": _reolink_time(dt.datetime(2026, 8, 4, 6, 30, tzinfo=dt.UTC)),
        "name": "1-4-0-01260704063000-00000",
        "size": 1024,
        "type": "sub",
    }
    assert serialize_file(_FakeVod(data, tz))["playback_derived"] is False


def test_a_device_stating_neither_playback_time_nor_a_name_is_named_by_timestamp() -> None:
    """reolink_aio's own fallback name is built from PlaybackTime, so it raises too.

    A device sending neither field would otherwise fail one line further on, in exactly the
    same way and with an equally opaque message.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    file = _hub_file(
        dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz),
        dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz),
        tz,
    )

    # The name the library would have derived, had it had the field to derive it from.
    assert serialize_file(file)["name"] == "20260804063000"


def test_a_device_reporting_no_offset_keeps_its_wall_clock() -> None:
    """A naive timestamp is the only time such a device has; converting it moves the request."""
    file = _hub_file(
        dt.datetime(2026, 8, 4, 8, 30),
        dt.datetime(2026, 8, 4, 8, 35),
        None,
        name="1-4-0-01260704063000-00000",
    )

    result = serialize_file(file)

    assert result["playback_id"] == "20260804083000"
    assert result["file_start_id"] == "20260804083000"
    assert result["offset"] == 0.0


def test_files_without_playback_time_abstain_from_the_convention_vote() -> None:
    """There is no convention to measure where the field is absent, and no row may guess."""
    tz = dt.timezone(dt.timedelta(hours=2))
    files = [
        _hub_file(
            dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz),
            dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz),
            tz,
            name="1-4-0-01260704063000-00000",
        ),
        _hub_file(
            dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz),
            dt.datetime(2026, 8, 4, 8, 40, tzinfo=tz),
            tz,
            name="1-4-0-01260704063500-00000",
        ),
    ]

    # Falls back to the library's assumption, and nothing consults it: every row derives.
    assert playback_time_is_utc(files) is True
    assert all(serialize_file(file, playback_is_utc=True)["playback_derived"] for file in files)


def test_a_derived_playback_time_follows_the_measured_convention() -> None:
    """A recorder stating its own time must be derived for in its own time too.

    The convention is measured from the rows that state the field and then applied to the
    whole search. A derived row that ignored it would sit an entire UTC offset away from
    the recorder on a device keeping local time, while the rows either side of it -- read
    from the same recorder, in the same search -- stayed right.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    file = _hub_file(
        dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz),
        dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz),
        tz,
        name="1-4-0-01260704063000-00000",
    )

    # Measured as keeping UTC: the endpoint wants the instant, so convert.
    assert serialize_file(file, playback_is_utc=True)["playback_id"] == "20260804063000"
    # Measured as keeping its own time: the endpoint wants the wall clock, unconverted --
    # which is what a stated row on this recorder would have been asked for.
    assert serialize_file(file, playback_is_utc=False)["playback_id"] == "20260804083000"

    # Either way the recording is addressed by its own start in the recorder's own time,
    # and this row is the whole recording.
    for flag in (True, False):
        result = serialize_file(file, playback_is_utc=flag)
        assert result["file_start_id"] == "20260804083000"
        assert result["offset"] == 0.0


def test_a_mixed_search_serialises_both_kinds() -> None:
    """One unusual row must not take the camera-day down with it.

    The measurement still runs on the rows that do state the field, and the ones that do
    not are derived — rather than the whole search failing on the first of them.
    """
    tz = dt.timezone(dt.timedelta(hours=2))
    stated = _FakeVod(
        {
            "StartTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 30, tzinfo=tz)),
            "EndTime": _reolink_time(dt.datetime(2026, 8, 4, 8, 35, tzinfo=tz)),
            "PlaybackTime": _reolink_time(dt.datetime(2026, 8, 4, 6, 30, tzinfo=dt.UTC)),
            "name": "1-4-0-01260704063000-00000",
            "size": 1024,
            "type": "sub",
        },
        tz,
    )
    missing = _hub_file(
        dt.datetime(2026, 8, 4, 9, 30, tzinfo=tz),
        dt.datetime(2026, 8, 4, 9, 35, tzinfo=tz),
        tz,
        name="1-4-0-01260704073000-00000",
    )

    results = [serialize_file(file) for file in (stated, missing)]

    assert [row["playback_derived"] for row in results] == [False, True]
    assert [row["playback_id"] for row in results] == ["20260804063000", "20260804073000"]


async def test_a_hub_day_survives_a_search_with_no_playback_time(hass) -> None:
    """End to end: the search itself must return rows, not raise.

    This is the failure as reported — the panel showing stale results and the bare word
    `'PlaybackTime'` where a reason should be. `async_search_day` is where the exception
    escaped to the cache, so it is where the regression is pinned.
    """
    from unittest.mock import patch as mock_patch

    class _NoPlaybackTime(FakeVodFile):
        @property
        def playback_time(self) -> dt.datetime:
            raise KeyError("PlaybackTime")

    start = dt.datetime(2026, 8, 3, 14, 0, tzinfo=TZ)
    api = FakeApi(
        is_hub=True,
        files={
            "sub": [
                _NoPlaybackTime(
                    start, start + dt.timedelta(seconds=30), triggers=VOD_trigger.PERSON
                )
            ]
        },
    )

    with mock_patch(
        "custom_components.reolink_stamina.vod.async_get_host",
        return_value=FakeHost(api),
    ):
        files, _ = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 8, 3), 5)

    assert len(files) == 1
    assert files[0]["playback_derived"] is True
    assert files[0]["file_start_id"] == "20260803140000"
    assert files[0]["playback_id"] == "20260803120000"
