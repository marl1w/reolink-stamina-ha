"""Keeping a model built, and answering questions against it.

Rebuilt from the whole journal rather than updated in place, once a night. That sounds
wasteful and is the opposite: a model that is only ever added to can never un-learn a
constant that turned out to be wrong, whereas one rebuilt from raw transitions picks up a
changed merge window, a changed bandwidth or a changed bucketing the very next night, across
every event ever collected. It is a linear pass in plain Python, and it happens while the
household is asleep.

Scoring an event is then a handful of lookups, which is why reads get *cheaper* as history
grows rather than more expensive.
"""

from __future__ import annotations

import logging
import time

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .events import Event, derive
from .journal import Journal
from .rates import Model, build
from .score import Score, calibrate, ready, score

_LOGGER = logging.getLogger(__name__)

# Not on the hour. Every integration in the world rebuilds something at 03:00, and this one
# has no reason to join them.
_REBUILD_HOUR = 3
_REBUILD_MINUTE = 17


class Analysis:
    """The current model, and the events it was built from."""

    def __init__(self, hass: HomeAssistant, journal: Journal) -> None:
        """Prepare an analysis, without building anything yet."""
        self._hass = hass
        self._journal = journal
        self._model = Model()
        self._events: list[Event] = []
        self._cancel: CALLBACK_TYPE | None = None

    @property
    def model(self) -> Model:
        """Return the model as it was last built."""
        return self._model

    @property
    def events(self) -> list[Event]:
        """Return the events the model was built from, oldest first."""
        return self._events

    def async_schedule(self) -> None:
        """Rebuild nightly."""
        self.async_cancel()
        self._cancel = async_track_time_change(
            self._hass,
            lambda _now: self._hass.async_create_task(self.async_rebuild()),
            hour=_REBUILD_HOUR,
            minute=_REBUILD_MINUTE,
            second=0,
        )

    def async_cancel(self) -> None:
        """Stop rebuilding."""
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    async def async_rebuild(self) -> Model:
        """Read the whole journal and learn from it again."""
        transitions = await self._journal.async_transitions()
        if not transitions:
            self._model, self._events = Model(), []
            return self._model

        now = time.time()
        events = derive(self._hass, transitions)
        model = build(events, now=now)
        calibrate(model, events)

        self._model, self._events = model, events
        _LOGGER.debug(
            "Relevance model rebuilt from %s transitions into %s events across %s cameras",
            len(transitions),
            len(events),
            len(model.per_camera),
        )
        return model

    def state(self, camera: str) -> str:
        """Return what the panel should say about this camera.

        Three answers, and the third is the one that gets forgotten: a camera can have months
        of days behind it and still too few detections to compare anything against.
        """
        profile = self._model.per_camera.get(camera)
        if profile is None or profile.events == 0:
            return "collecting"
        if ready(profile):
            return "active"
        return "collecting" if profile.days < 14.0 else "too_few_events"

    def coverage(self, camera: str) -> dict[str, float | int]:
        """Return how much is behind a camera, for the panel to say so plainly."""
        profile = self._model.per_camera.get(camera)
        if profile is None:
            return {"days": 0.0, "events": 0}
        return {"days": round(profile.days, 1), "events": profile.events}

    def window(
        self,
        *,
        since: float,
        until: float,
        camera: str | None = None,
        names: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[Event, Score]]:
        """Score the events in a window, oldest first.

        The whole list is walked rather than a slice of it, because the predecessor term needs
        what came before — and for the first event in any window, that is something outside
        it. Slicing first would tell every window that nothing fired before it began, which is
        a strong signal and a wrong one.

        Answers even while a camera is still collecting. The scores are meaningless then and
        `unusual` is false for all of them, but *what was collected* is not meaningless at all:
        showing it is what makes the feature worth installing on the first day rather than in
        a fortnight.
        """
        found: list[tuple[Event, Score]] = []
        previous: Event | None = None
        for event in self._events:
            if since <= event.started_at < until and (camera is None or event.camera == camera):
                found.append(
                    (
                        event,
                        score(event, self._model, previous=previous, names=names, labels=labels),
                    )
                )
            previous = event
        return found
