"""Tests for search, trigger decomposition, availability matching and pre-roll."""

from __future__ import annotations

import datetime as dt

from reolink_aio.typings import VOD_trigger

from custom_components.reolink_stamina.vod import (
    async_search_calendar,
    async_search_day,
    build_events,
    is_continuous_day,
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
        nvr_name="Test NVR",
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


def test_event_carries_camera_and_nvr_names() -> None:
    """Rows must be attributable when several NVRs are merged into one list."""
    event = _events([_file(14, 0)])[0]
    assert event["camera"] == "Driveway"
    assert event["nvr"] == "Test NVR"
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
        nvr_name="Test NVR",
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
        nvr_name="Test NVR",
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
        nvr_name="Test NVR",
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
        nvr_name="Test NVR",
        channel=0,
        camera={"name": "Driveway"},
        primary_stream="sub",
        primary_files=[file],
        other_files={},
        pre_roll_default=5,
    )

    assert sorted(events[0]["kinds"]) == ["person", "timer"]
