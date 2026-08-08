"""Turning raw transitions into the events a person would recognise.

The journal holds every state change verbatim, which is the right thing to store and the
wrong thing to count. A single arrival produces a scatter of them: several sensors fire
within a second of each other, the person walks out of frame and back in, and a flapping
sensor adds a few more for good measure. Counted as they lie, one person walking to a door
is six detections, and every rate built on that is wrong by a factor nobody can measure.

So events are *derived*, here, on the way out — never stored. That is the whole point of the
arrangement: the window that folds a flicker into its neighbour is a guess today, and when
real data says it should be twelve seconds rather than twenty, changing it re-reads six
months of history instead of invalidating it.

Merging is per camera and kind, not per entity. Reolink reports the same person through
several sensors — `person`, `crossline_person`, `intrusion_person` — and they map to one
subject on purpose. An event is open while *any* of them is on, and closes once they are all
clear and stay clear.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import datetime as dt
import logging

from homeassistant.const import STATE_ON, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from ..const import EVENT_MAX_SECONDS, EVENT_MERGE_SECONDS
from .journal import Transition

_LOGGER = logging.getLogger(__name__)

_MINUTES_PER_DAY = 1440
_HALF_DAY_MINUTES = _MINUTES_PER_DAY // 2


@dataclass(slots=True, frozen=True)
class Event:
    """One detection, as the panel would describe it."""

    camera: str
    kind: str
    started_at: float
    # None while the sensors are still on. An event that has not finished has no duration
    # either, and saying it lasted "until now" would be a guess that gets counted.
    ended_at: float | None
    duration: float | None
    # Local clock, 0…1439. What captures a household's schedule.
    minute_of_day: int
    # Minutes from sunset, wrapped to ±720, or None where the sun never sets or Home
    # Assistant has no location. What captures darkness, which the clock cannot: 18:00 is
    # daylight in June and night in December.
    solar_offset: int | None
    is_weekend: bool


def _wrap(minutes: float) -> int:
    """Fold a minute offset into ±720, so midnight is not a discontinuity."""
    value = round(minutes) % _MINUTES_PER_DAY
    if value >= _HALF_DAY_MINUTES:
        value -= _MINUTES_PER_DAY
    return value


class SolarClock:
    """Sunset for a date, remembered.

    Astral is cheap and a year of events is not, so each date is worked out once. Local
    dates, because that is what a household's evening is measured against.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Prepare a clock for this installation's location."""
        self._hass = hass
        # Per instance rather than an `lru_cache` on the method, which would key on `self`
        # and quietly keep every Home Assistant it had ever been asked about alive.
        self._sunsets: dict[dt.date, dt.datetime | None] = {}

    def _sunset(self, day: dt.date) -> dt.datetime | None:
        """Return sunset on a local date, or None where there is none to have."""
        if day not in self._sunsets:
            try:
                self._sunsets[day] = get_astral_event_date(self._hass, SUN_EVENT_SUNSET, day)
            except (ValueError, TypeError):
                # No location configured, or a latitude where the sun does not set today.
                self._sunsets[day] = None
        return self._sunsets[day]

    def offset(self, moment: dt.datetime) -> int | None:
        """Return minutes from that day's sunset, negative before it."""
        sunset = self._sunset(moment.date())
        if sunset is None:
            return None
        return _wrap((moment - sunset).total_seconds() / 60.0)


def _detecting(transition: Transition) -> bool:
    """Whether this state means the camera is seeing something.

    Anything that is not `on` — cleared, unavailable, unknown — means it is not. Unavailable
    is deliberately not treated as "still detecting": a camera that drops off the network
    mid-event has not been watching a person for six hours.
    """
    return transition.state == STATE_ON


def _runs(
    transitions: list[Transition], *, window: float, longest: float
) -> Iterator[tuple[float, float | None]]:
    """Yield the start and end of each merged run of detection."""
    start: float | None = None
    end: float | None = None
    active: set[str] = set()

    for row in transitions:
        if _detecting(row):
            if start is not None and end is not None and row.at - end > window:
                # Quiet for longer than the window, so the previous run really did finish.
                yield start, end
                start = None
            if start is None:
                start = row.at
                end = None
            active.add(row.entity_id)
        else:
            active.discard(row.entity_id)
            if not active and start is not None:
                # Provisional: another detection inside the window will reopen this.
                end = row.at

        if start is not None and row.at - start >= longest:
            yield start, end if end is not None else row.at
            start, end, active = None, None, set()

    if start is not None:
        yield start, end


def derive(
    hass: HomeAssistant,
    transitions: Iterable[Transition],
    *,
    window: float = EVENT_MERGE_SECONDS,
    longest: float = EVENT_MAX_SECONDS,
    clock: SolarClock | None = None,
) -> list[Event]:
    """Fold transitions into events, oldest first.

    `window` and `longest` are arguments rather than constants read inside, because the
    replay script exists to try other values against the same history.
    """
    clock = clock or SolarClock(hass)

    grouped: dict[tuple[str, str], list[Transition]] = {}
    for row in transitions:
        grouped.setdefault((row.camera, row.kind), []).append(row)

    events: list[Event] = []
    for (camera, kind), rows in grouped.items():
        rows.sort(key=lambda item: item.at)
        for started_at, ended_at in _runs(rows, window=window, longest=longest):
            local = dt_util.as_local(dt_util.utc_from_timestamp(started_at))
            events.append(
                Event(
                    camera=camera,
                    kind=kind,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration=None if ended_at is None else round(ended_at - started_at, 3),
                    minute_of_day=local.hour * 60 + local.minute,
                    solar_offset=clock.offset(local),
                    is_weekend=local.weekday() >= 5,
                )
            )

    events.sort(key=lambda item: (item.started_at, item.camera, item.kind))
    return events
