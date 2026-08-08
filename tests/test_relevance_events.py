"""Folding transitions into the events a person would recognise.

This is where the count comes from, so it is where a wrong answer does the most damage. One
person walking to a door produces a scatter of transitions — several sensors within a second
of each other, the person leaving frame and returning, a sensor flapping — and counting those
as separate detections would inflate every rate built on top by a factor nobody can measure.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.reolink_stamina.relevance.events import Event, derive
from custom_components.reolink_stamina.relevance.journal import Transition

_CAMERA = "entry1:0"


def _row(at: float, state: str, *, entity: str = "binary_sensor.drive_person", kind="person"):
    return Transition(
        camera=_CAMERA, entity_id=entity, kind=kind, state=state, at=at, source="live"
    )


async def test_one_detection_becomes_one_event(hass: HomeAssistant):
    """The simple case, and the shape everything else is measured against."""
    events = derive(hass, [_row(100.0, "on"), _row(108.0, "off")])

    assert len(events) == 1
    assert events[0].started_at == 100.0
    assert events[0].duration == 8.0


async def test_a_flicker_inside_the_window_is_one_event(hass: HomeAssistant):
    """A person leaving frame and stepping back is one arrival, not two."""
    events = derive(
        hass,
        [_row(100.0, "on"), _row(105.0, "off"), _row(110.0, "on"), _row(120.0, "off")],
        window=20.0,
    )

    assert len(events) == 1
    assert events[0].duration == 20.0


async def test_a_gap_beyond_the_window_is_two_events(hass: HomeAssistant):
    """Two separate visits an hour apart must not be counted as one long one."""
    events = derive(
        hass,
        [_row(100.0, "on"), _row(105.0, "off"), _row(400.0, "on"), _row(410.0, "off")],
        window=20.0,
    )

    assert [event.duration for event in events] == [5.0, 10.0]


async def test_the_window_is_an_argument_not_a_constant(hass: HomeAssistant):
    """It is a guess until real data sets it, so replaying with another value must work."""
    rows = [_row(100.0, "on"), _row(105.0, "off"), _row(140.0, "on"), _row(145.0, "off")]

    assert len(derive(hass, rows, window=20.0)) == 2
    assert len(derive(hass, rows, window=60.0)) == 1


async def test_sensors_reporting_the_same_subject_merge(hass: HomeAssistant):
    """Reolink reports one person through several sensors; they are one detection."""
    events = derive(
        hass,
        [
            _row(100.0, "on", entity="binary_sensor.drive_person"),
            _row(101.0, "on", entity="binary_sensor.drive_crossline_person"),
            _row(110.0, "off", entity="binary_sensor.drive_person"),
            _row(118.0, "off", entity="binary_sensor.drive_crossline_person"),
        ],
        window=5.0,
    )

    assert len(events) == 1
    assert events[0].duration == 18.0


async def test_different_kinds_stay_separate(hass: HomeAssistant):
    """A vehicle and a person arriving together are two facts, and both are interesting."""
    events = derive(
        hass,
        [
            _row(100.0, "on", kind="person"),
            _row(100.0, "on", entity="binary_sensor.drive_vehicle", kind="vehicle"),
            _row(110.0, "off", kind="person"),
            _row(110.0, "off", entity="binary_sensor.drive_vehicle", kind="vehicle"),
        ],
    )

    assert sorted(event.kind for event in events) == ["person", "vehicle"]


async def test_a_stuck_sensor_does_not_hold_an_event_open_for_ever(hass: HomeAssistant):
    """An event of unbounded length would poison the duration term for everything else."""
    rows = [_row(0.0, "on"), *[_row(float(t), "on") for t in range(60, 2000, 60)]]
    events = derive(hass, rows, longest=600.0)

    assert len(events) > 1
    assert all(event.duration is None or event.duration <= 600.0 for event in events)


async def test_an_open_event_has_no_duration(hass: HomeAssistant):
    """Still detecting is not the same as having lasted until now, which would be a guess."""
    events = derive(hass, [_row(100.0, "on")])

    assert events[0].ended_at is None
    assert events[0].duration is None


async def test_unavailable_ends_an_event(hass: HomeAssistant):
    """A camera that drops off the network has not been watching a person for six hours."""
    events = derive(hass, [_row(100.0, "on"), _row(110.0, "unavailable")], window=1.0)

    assert events[0].duration == 10.0


async def test_the_local_clock_is_what_gets_recorded(hass: HomeAssistant):
    """A household's schedule is in local time; UTC would smear it across the year."""
    moment = dt_util.as_local(dt_util.utc_from_timestamp(1_000_000_000.0))
    events = derive(hass, [_row(1_000_000_000.0, "on"), _row(1_000_000_010.0, "off")])

    assert events[0].minute_of_day == moment.hour * 60 + moment.minute
    assert events[0].is_weekend == (moment.weekday() >= 5)


async def test_events_come_back_oldest_first(hass: HomeAssistant):
    """The predecessor term walks them in order, so the order is part of the contract."""
    events = derive(
        hass,
        [
            _row(500.0, "on", entity="binary_sensor.b", kind="vehicle"),
            _row(505.0, "off", entity="binary_sensor.b", kind="vehicle"),
            _row(100.0, "on"),
            _row(105.0, "off"),
        ],
    )

    assert [event.started_at for event in events] == [100.0, 500.0]


async def test_no_transitions_is_no_events(hass: HomeAssistant):
    """A camera that has never fired is empty, not an error."""
    assert derive(hass, []) == []


async def test_solar_offset_is_measured_against_that_day(hass: HomeAssistant):
    """Sunset moves by hours across the year; a fixed hour would not track it."""
    winter = dt_util.as_timestamp("2026-01-15 20:00:00")
    summer = dt_util.as_timestamp("2026-07-15 20:00:00")
    events = derive(
        hass,
        [
            _row(winter, "on"),
            _row(winter + 5, "off"),
            _row(summer, "on", entity="binary_sensor.b"),
            _row(summer + 5, "off", entity="binary_sensor.b"),
        ],
        window=1.0,
    )

    offsets = [event.solar_offset for event in events]
    assert all(offset is not None for offset in offsets)
    # The same wall-clock hour is well after sunset in January and around it in July.
    assert offsets[0] != offsets[1]


def test_an_event_is_hashable_and_frozen():
    """They are keys and cache entries downstream, so nothing may mutate one."""
    event = Event(
        camera=_CAMERA,
        kind="person",
        started_at=1.0,
        ended_at=2.0,
        duration=1.0,
        minute_of_day=60,
        solar_offset=-30,
        is_weekend=False,
    )
    assert hash(event)
