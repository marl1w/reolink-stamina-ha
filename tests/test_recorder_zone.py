"""The recorder's own timezone, rebuilt from its DST rules (issue #14)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from reolink_aio.typings import VOD_trigger

from custom_components.reolink_stamina.recorder_zone import RecorderZone, recorder_zone
from custom_components.reolink_stamina.vod import async_search_day

from .conftest import FakeApi, FakeHost, FakeVodFile


def _settings(
    *,
    zone: int,
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
    enable: bool = True,
) -> dict:
    """Return a GetTime answer: (month, week, weekday from Sunday, hour) for each rule."""
    rules: dict = {"enable": enable, "offset": 1}
    for prefix, (month, week, weekday, hour) in (("start", start), ("end", end)):
        rules |= {
            f"{prefix}Mon": month,
            f"{prefix}Week": week,
            f"{prefix}Weekday": weekday,
            f"{prefix}Hour": hour,
            f"{prefix}Min": 0,
            f"{prefix}Sec": 0,
        }
    return {"Dst": rules, "Time": {"timeZone": zone}}


def _central_europe(week: int) -> dict:
    return _settings(zone=-3600, start=(3, week, 0, 2), end=(10, week, 0, 3))


def _offset(zone: dt.tzinfo, *args: int) -> dt.timedelta | None:
    return dt.datetime(*args, tzinfo=zone).utcoffset()


def test_the_day_of_the_report_is_still_summer_time() -> None:
    """19 October 2026 is a week before the clocks go back, whichever way the rule is set."""
    for week in (4, 5):
        zone = RecorderZone(_central_europe(week))
        assert _offset(zone, 2026, 10, 19, 13, 52) == dt.timedelta(hours=2)


def test_central_europe_changes_on_the_right_sundays() -> None:
    """The last Sunday of March and of October: 29 March and 25 October in 2026."""
    zone = RecorderZone(_central_europe(5))
    assert _offset(zone, 2026, 3, 29, 1, 59) == dt.timedelta(hours=1)
    assert _offset(zone, 2026, 3, 29, 2, 0) == dt.timedelta(hours=2)
    assert _offset(zone, 2026, 10, 25, 2, 59) == dt.timedelta(hours=2)
    assert _offset(zone, 2026, 10, 25, 3, 0) == dt.timedelta(hours=1)


def test_a_numbered_week_is_that_occurrence_of_the_weekday() -> None:
    """The 4th Sunday is the 4th Sunday, even in a March that has five of them."""
    zone = RecorderZone(_central_europe(4))
    assert _offset(zone, 2026, 3, 22, 2, 0) == dt.timedelta(hours=2)
    assert _offset(zone, 2026, 3, 21, 23, 0) == dt.timedelta(hours=1)
    assert _offset(zone, 2026, 10, 25, 3, 0) == dt.timedelta(hours=1)


def test_a_southern_summer_spans_the_new_year() -> None:
    """Sydney: first Sunday of October to first Sunday of April, on +10:00."""
    zone = RecorderZone(_settings(zone=-36000, start=(10, 1, 0, 2), end=(4, 1, 0, 3)))
    assert _offset(zone, 2026, 1, 15, 12, 0) == dt.timedelta(hours=11)
    assert _offset(zone, 2026, 6, 15, 12, 0) == dt.timedelta(hours=10)
    assert _offset(zone, 2026, 12, 15, 12, 0) == dt.timedelta(hours=11)


def test_converting_from_utc_lands_on_the_recorder_clock() -> None:
    """The default fromutc is driven by dst(), so the two have to agree."""
    zone = RecorderZone(_central_europe(4))
    moment = dt.datetime(2026, 10, 19, 11, 52, tzinfo=dt.UTC).astimezone(zone)
    assert (moment.hour, moment.minute) == (13, 52)
    assert zone.tzname(moment) == "UTC+02:00"


def test_without_summer_time_the_library_zone_stays() -> None:
    """A bare offset is the one thing reolink_aio cannot get wrong, so nothing is replaced."""
    api = FakeApi()
    assert recorder_zone(api) is None
    api._time_settings = _central_europe(4) | {"Dst": {"enable": False, "offset": 1}}
    assert recorder_zone(api) is None


async def test_a_search_states_recordings_in_the_recorder_offset(hass) -> None:
    """The recording at 13:52 on the recorder's clock is 13:52 at +02:00, not at +01:00."""
    wrong = dt.timezone(dt.timedelta(hours=1))
    start = dt.datetime(2026, 10, 19, 13, 52, tzinfo=wrong)
    api = FakeApi(
        files={
            "sub": [
                FakeVodFile(start, start + dt.timedelta(minutes=2), triggers=VOD_trigger.PERSON)
            ]
        }
    )
    api._time_settings = _central_europe(4)

    with patch("custom_components.reolink_stamina.vod.async_get_host", return_value=FakeHost(api)):
        files, _ = await async_search_day(hass, "entry", 0, "sub", dt.date(2026, 10, 19), 0)

    assert files[0]["start"] == "2026-10-19T13:52:00+02:00"
    assert files[0]["end"] == "2026-10-19T13:54:00+02:00"
