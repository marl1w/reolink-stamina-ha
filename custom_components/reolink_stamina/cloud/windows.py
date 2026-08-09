"""Turning detections into clips worth uploading.

One arrival trips several sensors at once — a car sets off motion, vehicle and often person
inside a second — and a person crossing a garden goes in and out of frame repeatedly. Left
alone that is five uploads of the same twenty seconds.

So detections accumulate into a *window* per camera, and the rules are about presence rather
than about events:

* The window opens when the first sensor turns on.
* It stays open for as long as **any** sensor on that camera is still on, however long that
  is. A person standing in frame for two minutes is one clip, not one clip and then silence.
* When the last sensor clears, the tail begins. Anything that turns on again during the tail
  belongs to the same clip and starts the tail over — someone stepping out of frame and back
  is one visit, not two.
* A sensor that sticks on cannot hold a clip hostage for ever, so a window is closed at a
  hard maximum regardless.
* Whether an event is taken on at all is decided **once, as it opens**. A window that opened
  while clips were being accepted belongs to the cloud however the switch stands when it
  finally settles, which is the better part of a minute later. The reverse holds just as
  firmly: a window that opened while they were *not* being accepted cannot become an
  ordinary upload because the switch came back on halfway through it. That is what
  `admitted` records.

Deliberately pure: it holds no timers and talks to nothing, so the rules can be tested by
moving a clock by hand rather than by waiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt


@dataclass(slots=True)
class OpenWindow:
    """Detections gathered for one camera, not yet finished."""

    first: dt.datetime
    # The last moment anything happened — a sensor turning on *or* clearing. The tail is
    # measured from here, which is why it has to include the clearing.
    last_activity: dt.datetime
    # Sensors currently reading on. While this is non-empty the event is still happening.
    active: set[str] = field(default_factory=set)
    kinds: set[str] = field(default_factory=set)
    truncated: bool = False
    # Whether the switch was on at the moment this opened.
    admitted: bool = True


@dataclass(slots=True, frozen=True)
class ClipWindow:
    """A finished stretch of footage to fetch and upload."""

    key: str
    start: dt.datetime
    end: dt.datetime
    kinds: tuple[str, ...]
    # True when the window was closed by the safety limit rather than by the camera going
    # quiet, so the caller can say the clip is the first part of something longer.
    truncated: bool = False
    # Whether this camera's switch was on when the event opened. False for a window gathered
    # only because that recorder uploads unusual events regardless of the switch — such a
    # window may only ever become an unusual upload, never an ordinary one.
    admitted: bool = True

    @property
    def seconds(self) -> float:
        """Return the length of the clip."""
        return (self.end - self.start).total_seconds()


class WindowCollector:
    """Gathers detections per camera and hands over clips once they settle."""

    def __init__(
        self,
        *,
        lead: float,
        tail: float,
        settle: float,
        maximum: float,
    ) -> None:
        """Configure the padding, how long quiet means finished, and the hard ceiling."""
        self._lead = lead
        self._tail = tail
        self._settle = settle
        self._maximum = maximum
        self._open: dict[str, OpenWindow] = {}

    @property
    def pending(self) -> int:
        """Return the number of cameras with a window still gathering."""
        return len(self._open)

    def active(self, key: str) -> bool:
        """Whether this camera currently has something in frame."""
        window = self._open.get(key)
        return bool(window and window.active)

    def next_due(self) -> dt.datetime | None:
        """Return the earliest moment any open window could become collectable.

        What a caller uses to look again at exactly the right time instead of sweeping. A
        window with nothing on any more is due when its tail elapses; one still holding a
        sensor on can only be closed by the maximum, so that is when it is worth looking.

        None when nothing is open, which means there is nothing to wait for.
        """
        if not self._open:
            return None
        due: list[dt.datetime] = []
        for window in self._open.values():
            if window.active:
                due.append(window.first + dt.timedelta(seconds=self._maximum))
            else:
                due.append(window.last_activity + dt.timedelta(seconds=self._settle))
        return min(due)

    def record_on(
        self,
        key: str,
        sensor: str,
        kind: str,
        moment: dt.datetime,
        *,
        accepting: bool = True,
        admitted: bool | None = None,
    ) -> bool:
        """Note that a detection sensor turned on; return whether it was taken on.

        `accepting` is consulted only to *open* a window. Once one exists the event has been
        admitted and every later detection on that camera belongs to it, whatever the switch
        does in the meantime — including a sensor coming back on during the tail.

        Asking again when the window closes is what used to lose an arrival: the clip settles
        some forty seconds after the first detection, by which time the alarm has been
        disarmed and the switch turned off, and the footage of the arrival was dropped.

        `admitted` separates "gather this" from "the switch was on", which are the same
        question only for a recorder that syncs nothing but the kinds it was given. A recorder
        that also uploads unusual events gathers whatever the switch says and sorts it out
        later, and defaults to `accepting` for everyone else.
        """
        window = self._open.get(key)
        if window is None:
            if not accepting:
                return False
            window = OpenWindow(
                first=moment,
                last_activity=moment,
                admitted=accepting if admitted is None else admitted,
            )
            self._open[key] = window
        window.first = min(window.first, moment)
        window.last_activity = max(window.last_activity, moment)
        window.active.add(sensor)
        window.kinds.add(kind)
        return True

    def record_off(self, key: str, sensor: str, moment: dt.datetime) -> None:
        """Note that a detection sensor cleared.

        This is what starts the tail: the clip should end a margin after the last thing left,
        not a margin after it was first seen.
        """
        window = self._open.get(key)
        if window is None:
            return
        window.active.discard(sensor)
        window.last_activity = max(window.last_activity, moment)

    def collect(self, now: dt.datetime) -> list[ClipWindow]:
        """Return the windows that are finished.

        A window is finished when nothing is on any more and the tail has elapsed — or when it
        has run past the maximum, which stops one stuck sensor from suppressing every upload
        for that camera indefinitely.
        """
        ready: list[ClipWindow] = []
        for key, window in list(self._open.items()):
            overdue = (now - window.first).total_seconds() >= self._maximum
            quiet = (
                not window.active and (now - window.last_activity).total_seconds() >= self._settle
            )
            if not (overdue or quiet):
                continue
            del self._open[key]
            # A truncated window ends now; a settled one ends when the camera went quiet.
            finish = now if overdue and window.active else window.last_activity
            ready.append(
                ClipWindow(
                    key=key,
                    start=window.first - dt.timedelta(seconds=self._lead),
                    end=finish + dt.timedelta(seconds=self._tail),
                    # Ordered so a name or a log line reads the same way twice.
                    kinds=tuple(sorted(window.kinds)),
                    truncated=overdue,
                    admitted=window.admitted,
                )
            )
        return ready

    def flush(self, now: dt.datetime) -> list[ClipWindow]:
        """Finish every open window regardless of quiet, for shutdown.

        Better a slightly short clip of something that just happened than none at all because
        Home Assistant restarted while a cat was in frame.
        """
        ready: list[ClipWindow] = []
        for key, window in list(self._open.items()):
            del self._open[key]
            ready.append(
                ClipWindow(
                    key=key,
                    start=window.first - dt.timedelta(seconds=self._lead),
                    end=max(window.last_activity, now) + dt.timedelta(seconds=self._tail),
                    kinds=tuple(sorted(window.kinds)),
                    truncated=bool(window.active),
                    admitted=window.admitted,
                )
            )
        return ready
