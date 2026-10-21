"""The recorder's own timezone, worked out from the rules it was configured with.

A recorder states every recording as a bare wall clock, and reolink_aio attaches the zone
that turns it into an instant. That zone is built from the recorder's DST rules -- a month,
a week of it, a weekday and a time, the same `Mm.w.d` shape as a POSIX TZ string -- and
reolink_aio resolves the rule to the wrong date. For Central Europe in 2026 a "4th Sunday of
October" rule comes back as Monday 19 October, and a "last Sunday" rule as Saturday 21 March
to Sunday 1 November. Every recording made in between is then labelled an hour away from
the recorder's own correct clock: the panel lists it an hour out from the timestamp burnt
into the video, and every detection matched against it by instant misses by the same hour.
That is issue #14, reported on 19 October from a recorder set up correctly.

So the zone is rebuilt here from the same settings, resolving each rule the POSIX way:
weeks 1-4 are that occurrence of the weekday, 5 is the last one in the month. It also
handles a southern-hemisphere rule, where summer time starts in one year and ends in the
next, which a plain `start <= moment <= end` comparison reads as never.

The settings are a private attribute of the library's Host; the upstream contract tests say
so the moment that stops holding, and until then a recorder that answers without them keeps
whatever zone reolink_aio gave it.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any


def _transition(rules: dict[str, Any], prefix: str, year: int) -> dt.datetime:
    """Return the local wall clock a DST rule fires at in a year."""
    month = rules[f"{prefix}Mon"]
    week = min(max(rules[f"{prefix}Week"], 1), 5)
    # Reolink counts weekdays from Sunday, Python from Monday.
    weekday = (rules[f"{prefix}Weekday"] - 1) % 7
    if week == 5:
        day = dt.date(year, month, calendar.monthrange(year, month)[1])
        day -= dt.timedelta(days=(day.weekday() - weekday) % 7)
    else:
        day = dt.date(year, month, 1)
        day += dt.timedelta(days=(weekday - day.weekday()) % 7 + 7 * (week - 1))
    return dt.datetime.combine(
        day, dt.time(rules[f"{prefix}Hour"], rules[f"{prefix}Min"], rules[f"{prefix}Sec"])
    )


class RecorderZone(dt.tzinfo):
    """A fixed standard offset, plus summer time between two yearly rules."""

    def __init__(self, settings: dict[str, Any]) -> None:
        """Build the zone from a recorder's GetTime answer."""
        super().__init__()
        self._rules = settings["Dst"]
        # Reolink states the offset west of UTC in seconds; Python wants it east.
        self._standard = dt.timedelta(seconds=-settings["Time"]["timeZone"])
        self._summer = dt.timedelta(hours=self._rules["offset"])
        self._years: dict[int, tuple[dt.datetime, dt.datetime]] = {}

    def _in_summer(self, moment: dt.datetime) -> bool:
        local = moment.replace(tzinfo=None)
        if local.year not in self._years:
            self._years[local.year] = (
                _transition(self._rules, "start", local.year),
                _transition(self._rules, "end", local.year),
            )
        start, end = self._years[local.year]
        if start <= end:
            return start <= local < end
        return local >= start or local < end

    def utcoffset(self, moment: dt.datetime | None) -> dt.timedelta:
        """Return the offset in force at a local wall clock."""
        return self._standard + self.dst(moment)

    def dst(self, moment: dt.datetime | None) -> dt.timedelta:
        """Return the summer-time adjustment in force at a local wall clock."""
        if moment is None or not self._in_summer(moment):
            return dt.timedelta(0)
        return self._summer

    def tzname(self, moment: dt.datetime | None) -> str:
        """Return the offset as a name, the way the recorder itself has no better one."""
        offset = self.utcoffset(moment)
        sign = "-" if offset < dt.timedelta(0) else "+"
        hours, rest = divmod(abs(offset), dt.timedelta(hours=1))
        return f"UTC{sign}{hours:02d}:{rest // dt.timedelta(minutes=1):02d}"


def recorder_zone(api: Any) -> dt.tzinfo | None:
    """Return the recorder's zone, or None to keep the one reolink_aio attached.

    None too when summer time is off: a bare offset is the one thing the library cannot get
    wrong, and nothing would change.
    """
    settings = getattr(api, "_time_settings", None)
    try:
        if not settings or not settings["Dst"]["enable"]:
            return None
        return RecorderZone(settings)
    except (KeyError, TypeError, ValueError):
        return None


def rezoned(moment: dt.datetime, zone: dt.tzinfo | None) -> dt.datetime:
    """Return the same wall clock in the recorder's zone; unchanged without one, or if naive."""
    if zone is None or moment.tzinfo is None:
        return moment
    return moment.replace(tzinfo=zone)
