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
subject on purpose, so an arrival is one event however many of them report it.

How long it lasted is measured from the sensor that opened it, though, and not from the last
of them to clear. `linger_person` and its siblings stay on for as long as something lingers,
which turns a car parking in a garage into an event lasting hours. See `_runs`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import datetime as dt
import json
import logging

from homeassistant.const import STATE_ON, SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from ..const import EVENT_MAX_SECONDS, EVENT_MERGE_SECONDS
from .journal import Transition

_LOGGER = logging.getLogger(__name__)

# How far either side of sunrise or sunset still counts as twilight, for the phrase alone.
TWILIGHT_MINUTES = 30

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

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
    # "dawn", "day", "dusk", "night", or None where the offset is None. Carried beside the
    # offset rather than derived from it, because the offset alone cannot tell morning from
    # evening: 07:00 and 21:00 are both hours away from sunset.
    solar_phase: str | None
    is_weekend: bool
    # "Mon" … "Sun". Beside the weekend flag rather than derived from it, because the two are
    # counted separately and blended: see `Profile.day_of_week`.
    day_of_week: str = ""
    # The configured signals as they stood when this began, or None where none are set up.
    # Raw states: what a value means is a question for whoever reads it back.
    context: tuple[tuple[str, str], ...] = ()


def _wrap(minutes: float) -> int:
    """Fold a minute offset into ±720, so midnight is not a discontinuity."""
    value = round(minutes) % _MINUTES_PER_DAY
    if value >= _HALF_DAY_MINUTES:
        value -= _MINUTES_PER_DAY
    return value


class SolarClock:
    """Where the sun was, remembered per date.

    Astral is cheap and a year of events is not, so each date is worked out once. Local
    dates, because that is what a household's evening is measured against.

    Two answers rather than one, and they do different jobs. The *offset* from sunset is what
    the model counts: it is continuous, it wraps, and it tracks the season in a way the clock
    cannot — 18:00 is daylight in June and night in December. The *phase* is what a person
    reads, because "8h 53m before sunset" is arithmetically correct and means nothing; the
    honest way to say it is "in daylight".
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Prepare a clock for this installation's location."""
        self._hass = hass
        # Per instance rather than an `lru_cache` on the method, which would key on `self`
        # and quietly keep every Home Assistant it had ever been asked about alive.
        self._events: dict[tuple[str, dt.date], dt.datetime | None] = {}

    def _event(self, name: str, day: dt.date) -> dt.datetime | None:
        """Return a solar event on a local date, or None where there is none to have."""
        key = (name, day)
        if key not in self._events:
            try:
                self._events[key] = get_astral_event_date(self._hass, name, day)
            except (ValueError, TypeError):
                # No location configured, or a latitude where the sun does not set today.
                self._events[key] = None
        return self._events[key]

    def offset(self, moment: dt.datetime) -> int | None:
        """Return minutes from that day's sunset, negative before it."""
        sunset = self._event(SUN_EVENT_SUNSET, moment.date())
        if sunset is None:
            return None
        return _wrap((moment - sunset).total_seconds() / 60.0)

    def phase(self, moment: dt.datetime) -> str | None:
        """Return "dawn", "day", "dusk" or "night" — what somebody would call it.

        Twilight is claimed by whichever edge it is nearer, within a fixed half hour either
        side. Not civil twilight, which varies from twenty minutes to never depending on the
        latitude and would make the same phrase mean different things in different houses.
        """
        day = moment.date()
        sunrise = self._event(SUN_EVENT_SUNRISE, day)
        sunset = self._event(SUN_EVENT_SUNSET, day)
        if sunrise is None or sunset is None:
            return None

        edge = dt.timedelta(minutes=TWILIGHT_MINUTES)
        if abs(moment - sunrise) <= edge:
            return "dawn"
        if abs(moment - sunset) <= edge:
            return "dusk"
        return "day" if sunrise < moment < sunset else "night"


def _signals(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Turn a stored snapshot back into pairs, sorted so two events compare equal.

    A tuple rather than a dict because an Event is frozen and hashed, and JSON that cannot be
    read is treated as no signals at all: a corrupt row should cost its own context and
    nothing else.
    """
    if not raw:
        return ()
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(found, dict):
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in found.items()))


def _detecting(transition: Transition) -> bool:
    """Whether this state means the camera is seeing something.

    Anything that is not `on` — cleared, unavailable, unknown — means it is not. Unavailable
    is deliberately not treated as "still detecting": a camera that drops off the network
    mid-event has not been watching a person for six hours.
    """
    return transition.state == STATE_ON


def _runs(
    transitions: list[Transition], *, window: float, longest: float
) -> Iterator[tuple[float, float | None, str | None]]:
    """Yield the start, end and opening context of each merged run of detection.

    A run belongs to the sensor that opened it, and ends when *that* sensor clears.

    It used to stay open while any sensor of the same kind was on, which is defensible right
    up until you look at what Reolink actually publishes. `vehicle`, `crossline_vehicle` and
    `linger_vehicle` all map to one subject on purpose — but `linger_vehicle` stays on for as
    long as something lingers, so a car pulling into a garage held the whole event open behind
    it. Measured on a real installation: three transitions in isolation derive to 10.9
    seconds; add one same-kind sensor bouncing nearby and the same arrival becomes 288.2.

    Everything a companion sensor does *inside* an open run is still swallowed, which is the
    part worth keeping: one arrival remains one event however many sensors report it. It
    simply no longer decides when that event finished.
    """
    start: float | None = None
    end: float | None = None
    context: str | None = None
    opener: str | None = None

    for row in transitions:
        if _detecting(row):
            if start is not None and end is not None and row.at - end > window:
                # Quiet for longer than the window, so the previous run really did finish.
                yield start, end, context
                start = None
            if start is None:
                start = row.at
                end = None
                opener = row.entity_id
                # The signals as they were when it *began*, not when it cleared: what was
                # true at the start is what the event happened against.
                context = row.context
            elif row.entity_id == opener:
                # The same sensor flickering back on inside the window: one detection, still
                # running, so whatever end it had recorded was premature.
                end = None
        elif start is not None and end is None and row.entity_id == opener:
            # The *first* clear ends it. Provisional only in that the same sensor firing again
            # inside the window reopens it, above.
            #
            # `end is None` is the whole guard, and leaving it out is how a seven-second animal
            # detection came to last five minutes. A run is not yielded until something opens
            # the next one, so a later redundant clear — a second `off`, an `unavailable` when
            # the recorder reboots — used to land here and drag the end along with it, with
            # nothing on this path checking the merge window. The cap then trimmed the result
            # to exactly `EVENT_MAX_SECONDS`, which is why every one of these was 300 seconds
            # to the millisecond: the giveaway that it was arithmetic rather than a detection.
            end = row.at

        if start is not None and row.at - start >= longest:
            # Cut at the limit, not at whenever the next transition happened to arrive.
            #
            # This yielded `row.at`, so a sensor that stuck on and went quiet produced an
            # event as long as the silence: a real installation had vehicle detections
            # lasting two and a quarter hours, which then read as the rarest duration that
            # camera had ever seen and pushed the event over the line on its own.
            yield start, min(end if end is not None else row.at, start + longest), context
            start, end, context, opener = None, None, None, None

    if start is not None:
        yield start, end, context


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
        for started_at, ended_at, context in _runs(rows, window=window, longest=longest):
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
                    solar_phase=clock.phase(local),
                    is_weekend=local.weekday() >= 5,
                    day_of_week=_DAYS[local.weekday()],
                    context=_signals(context),
                )
            )

    events.sort(key=lambda item: (item.started_at, item.camera, item.kind))
    return events
