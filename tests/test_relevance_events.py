"""Folding transitions into the events a person would recognise.

This is where the count comes from, so it is where a wrong answer does the most damage. One
person walking to a door produces a scatter of transitions — several sensors within a second
of each other, the person leaving frame and returning, a sensor flapping — and counting those
as separate detections would inflate every rate built on top by a factor nobody can measure.
"""

from __future__ import annotations

import datetime as dt

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from custom_components.reolink_stamina.relevance.events import Event, SolarClock, derive
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
    # Ten, not eighteen: the companion sensor is part of the same detection but does not get
    # to say when it ended. See the next test for why that distinction is the whole point.
    assert events[0].duration == 10.0


async def test_a_lingering_companion_sensor_does_not_hold_an_event_open(hass: HomeAssistant):
    """A car parking in a garage lasts seconds, not the minutes it then sits there.

    `linger_vehicle` maps to the same subject as `vehicle` and stays on for as long as
    something lingers. While a run stayed open for whichever same-kind sensor cleared last,
    one arrival on a real installation derived to 288 seconds instead of 11 — which then read
    as the rarest duration that camera had ever seen and marked the event on its own.
    """
    events = derive(
        hass,
        [
            _row(100.0, "on", entity="binary_sensor.drive_vehicle", kind="vehicle"),
            _row(102.0, "on", entity="binary_sensor.drive_linger_vehicle", kind="vehicle"),
            _row(111.0, "off", entity="binary_sensor.drive_vehicle", kind="vehicle"),
            # Still parked, five minutes later.
            _row(400.0, "off", entity="binary_sensor.drive_linger_vehicle", kind="vehicle"),
        ],
        window=3.0,
    )

    assert len(events) == 1, "still one arrival, however many sensors reported it"
    assert events[0].duration == 11.0


async def test_a_second_clear_does_not_drag_the_end_along_with_it(hass: HomeAssistant):
    """The first clear ends a run. A later one is not a longer detection.

    A run is not yielded until something opens the next one, so a redundant clear — a second
    `off`, or an `unavailable` when the recorder reboots — used to land on the closing path
    and move the end with it, and nothing on that path checked the merge window. The cap then
    trimmed the result to exactly the maximum, which is why a real installation reported a
    seven-second animal detection as five minutes to the millisecond.
    """
    events = derive(
        hass,
        [
            _row(100.0, "on"),
            _row(107.6, "off"),
            # Half an hour later, the same sensor reports itself clear again.
            _row(1900.0, "off"),
            _row(1901.0, "unavailable"),
        ],
        window=3.0,
        longest=300.0,
    )

    assert [event.duration for event in events] == [7.6]


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


async def test_the_solar_phase_names_the_part_of_the_day(hass: HomeAssistant):
    """Midday is "in daylight", not "eight hours before sunset".

    The offset is measured from sunset alone, which is the right thing to count and the wrong
    thing to read: it makes every lunchtime detection report a distance to an event nine hours
    away, and it cannot tell morning from evening at all.
    """
    # Asked of the same clock the code uses, rather than assumed: the test installation's
    # location is not this test's business, and a hardcoded 05:50 would only be dawn in one.
    clock = SolarClock(hass)
    day = dt.date(2026, 7, 15)
    sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, day)
    sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, day)
    assert sunrise is not None and sunset is not None

    def _phase(moment: dt.datetime) -> str | None:
        return clock.phase(dt_util.as_local(moment))

    noon = sunrise + (sunset - sunrise) / 2
    assert _phase(noon) == "day"
    assert _phase(sunset + dt.timedelta(hours=3)) == "night"
    assert _phase(sunrise + dt.timedelta(minutes=10)) == "dawn"
    assert _phase(sunset - dt.timedelta(minutes=10)) == "dusk"

    # And it reaches an Event, which is the only reason the clock exists.
    at = (noon).timestamp()
    found = derive(hass, [_row(at, "on"), _row(at + 5, "off")], window=1.0)
    assert found[0].solar_phase == "day"


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
        solar_phase="dusk",
        is_weekend=False,
    )
    assert hash(event)


async def test_a_stuck_sensor_is_cut_at_the_limit_not_at_the_next_transition(hass: HomeAssistant):
    """A sensor that sticks on and goes quiet must not invent an event as long as the silence.

    It used to end the run wherever the next transition happened to land, so a real
    installation recorded vehicle detections lasting two and a quarter hours — which then
    scored as the rarest duration that camera had ever seen.
    """
    events = derive(
        hass,
        # On, then nothing at all for three hours, then off.
        [_row(0.0, "on"), _row(10_800.0, "off")],
        longest=600.0,
    )

    assert events
    assert all(event.duration is not None and event.duration <= 600.0 for event in events), (
        f"durations ran past the limit: {[event.duration for event in events]}"
    )
