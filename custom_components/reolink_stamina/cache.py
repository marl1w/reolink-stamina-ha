"""Stale-while-revalidate cache for Reolink recording searches.

Searching a device is slow — seconds per camera per day, sometimes much worse on a busy
24/7 recorder. The panel must never wait on it. The rules this module implements:

* A cached answer is served **immediately**, however old it is.
* A refresh runs in the background and pushes a patch when it lands.
* A failed refresh never discards good cached data; it is reported alongside it.
* Identical concurrent requests share one fetch, and each device has a hard concurrency
  cap so the panel cannot overwhelm the recorder.
* A day in the past is immutable once recorded, so it is cached for a week. Only today
  expires quickly.

Results are persisted, so a restart still paints instantly from disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import datetime as dt
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    FILE_SCHEMA_VERSION,
    MAX_CONCURRENT_SEARCHES,
    SEARCH_WINDOW_DAYS,
    STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    TTL_PAST,
    TTL_TODAY,
)
from .reolink_registry import (
    DeviceUnavailableError,
    ReolinkIncompatibleError,
    async_paired_channel,
)
from .vod import async_search_calendar, async_search_day

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _SearchOutcome:
    """What one device said when asked for one camera-day.

    A result rather than an exception, because a paired camera asks two devices at once and
    one of them failing is not the camera failing. The old single-device path raised, and
    reporting "could not reach the device" because the copy nobody is using is offline would
    be a worse answer than the one the other device just gave.
    """

    target: tuple[str, int]
    files: list[dict[str, Any]] | None = None
    unlabelled: int = 0
    error: str | None = None


# A refresh that failed is retried sooner than a successful one, but not so soon that
# an offline device gets hammered.
TTL_ERROR = 30.0


def _age(fetched_at: float) -> float:
    """Seconds since a record was fetched, or infinity if that cannot be trusted.

    Wall clock, not time.time(): these timestamps are persisted and compared after
    restarts, and a monotonic clock counts from boot. After a host reboot a stored
    monotonic value yields a negative age, which would make stale data look fresh
    forever. A clock that has moved backwards is treated as stale instead.
    """
    if not fetched_at:
        return float("inf")
    age = time.time() - fetched_at
    return age if age >= 0 else float("inf")


def day_key(
    entry_id: str, channel: int, stream: str, date: dt.date, include_unlabelled: bool = False
) -> str:
    """Cache key for one camera-day-stream search.

    The unlabelled flag is part of the key because a cached entry recorded with them
    discarded is not a valid answer to a request that wants them.
    """
    suffix = "|all" if include_unlabelled else ""
    return f"{entry_id}|{channel}|{stream}|{date.isoformat()}{suffix}"


def calendar_key(entry_id: str, channel: int, year: int, month: int) -> str:
    """Cache key for one camera-month recording calendar."""
    return f"{entry_id}|{channel}|{year:04d}-{month:02d}"


@dataclass(slots=True)
class DayRecord:
    """Raw search result for one camera, one stream, one day."""

    files: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: float = 0.0
    error: str | None = None
    # Unlabelled continuous recordings dropped before storage, for reporting only.
    unlabelled: int = 0
    # Shape of the records in `files`; anything older is refetched.
    schema: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {
            "files": self.files,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "unlabelled": self.unlabelled,
            "schema": self.schema,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayRecord:
        """Restore from storage."""
        return cls(
            files=data.get("files") or [],
            fetched_at=float(data.get("fetched_at") or 0.0),
            error=data.get("error"),
            unlabelled=int(data.get("unlabelled") or 0),
            schema=int(data.get("schema") or 0),
        )


@dataclass(slots=True)
class CalendarRecord:
    """Which days of a month contain recordings."""

    days: list[int] = field(default_factory=list)
    fetched_at: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {"days": self.days, "fetched_at": self.fetched_at, "error": self.error}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalendarRecord:
        """Restore from storage."""
        return cls(
            days=data.get("days") or [],
            fetched_at=float(data.get("fetched_at") or 0.0),
            error=data.get("error"),
        )


class VodCache:
    """Persistent, coalescing, stale-while-revalidate cache of recording searches."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the cache."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._days: dict[str, DayRecord] = {}
        self._calendars: dict[str, CalendarRecord] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # Which device turned out to hold each paired camera's recordings. Learned from the
        # first search that returns any, and deliberately not persisted: it is one extra
        # empty search to relearn, against a stored answer outliving a change in how the
        # camera records.
        self._sources: dict[tuple[str, int], tuple[str, int]] = {}
        self._listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------ storage

    async def async_load(self) -> None:
        """Load persisted results from disk."""
        data = await self._store.async_load()
        if not data:
            return
        for key, raw in (data.get("days") or {}).items():
            try:
                self._days[key] = DayRecord.from_dict(raw)
            except (TypeError, ValueError, AttributeError):
                _LOGGER.debug("Discarding malformed cached day %s", key)
        for key, raw in (data.get("calendars") or {}).items():
            try:
                self._calendars[key] = CalendarRecord.from_dict(raw)
            except (TypeError, ValueError, AttributeError):
                _LOGGER.debug("Discarding malformed cached calendar %s", key)
        self._prune()
        _LOGGER.debug(
            "Loaded %s cached camera-days and %s calendars",
            len(self._days),
            len(self._calendars),
        )

    def _prune(self) -> None:
        """Drop anything older than the device's own search window.

        Both halves of the cache, on the same cutoff: a month whose last day has fallen out
        of the search window can never be asked about again, so keeping its calendar only
        grows the file on disk for the life of the installation.
        """
        cutoff = dt_util.now().date() - dt.timedelta(days=SEARCH_WINDOW_DAYS + 1)
        for key in list(self._days):
            parts = key.split("|")
            try:
                # ...|<stream>|<date>[|all]
                date = dt.date.fromisoformat(parts[3])
            except (IndexError, ValueError):
                del self._days[key]
                continue
            if date < cutoff:
                del self._days[key]

        for key in list(self._calendars):
            parts = key.split("|")
            try:
                # ...|<channel>|<YYYY>-<MM>
                year, month = (int(part) for part in parts[2].split("-"))
                # The month is only spent once its *last* day is out of reach.
                last_day = dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
            except (IndexError, ValueError):
                del self._calendars[key]
                continue
            if last_day < cutoff:
                del self._calendars[key]

    @callback
    def _schedule_save(self) -> None:
        """Persist soon, batching bursts of updates into one write."""
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        """Build the payload written to disk."""
        self._prune()
        return {
            "days": {key: record.as_dict() for key, record in self._days.items()},
            "calendars": {key: record.as_dict() for key, record in self._calendars.items()},
        }

    async def async_clear(self) -> None:
        """Forget everything and delete the persisted copy."""
        self._days.clear()
        self._calendars.clear()
        await self._store.async_remove()

    # ---------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback invoked with the key of every updated record."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self, key: str) -> None:
        """Tell subscribers a record changed."""
        for listener in list(self._listeners):
            try:
                listener(key)
            except Exception:
                _LOGGER.exception("Error in cache listener")

    @callback
    def _finish(self, key: str) -> None:
        """Retire an in-flight fetch, then tell subscribers.

        Order matters. Subscribers recompose their payload from the cache the moment they
        are called, and that payload carries `async_is_fetching(key)`. Notifying from
        inside the still-running task left every subscriber's *final* answer for that key
        saying a refresh was in flight — and no further notification was coming to correct
        it, so the panel's "Updating…" pill never went away.
        """
        self._tasks.pop(key, None)
        self._notify(key)

    # ------------------------------------------------------------------ reading

    @callback
    def peek_day(
        self,
        entry_id: str,
        channel: int,
        stream: str,
        date: dt.date,
        include_unlabelled: bool = False,
    ) -> DayRecord | None:
        """Return the cached record for a camera-day, without fetching."""
        return self._days.get(day_key(entry_id, channel, stream, date, include_unlabelled))

    @callback
    def sample_files(self, limit: int = 3) -> list[dict[str, Any]]:
        """Return a few cached recordings, for diagnostics rather than for the panel.

        Whatever happens to be cached, from whichever camera-days are held: this exists to
        show the timestamps a recording was described by, and any recording will do.
        """
        samples: list[dict[str, Any]] = []
        for key, record in self._days.items():
            for file in record.files:
                samples.append({"key": key, **file})
                if len(samples) >= limit:
                    return samples
        return samples

    @callback
    def peek_calendar(
        self, entry_id: str, channel: int, year: int, month: int
    ) -> CalendarRecord | None:
        """Return the cached recording calendar for a month, without fetching."""
        return self._calendars.get(calendar_key(entry_id, channel, year, month))

    @staticmethod
    def _ttl_for(date: dt.date, errored: bool) -> float:
        """How long a result for this date stays fresh."""
        if errored:
            return TTL_ERROR
        # Today is still being recorded into; the past cannot change.
        return TTL_TODAY if date >= dt_util.now().date() else TTL_PAST

    @callback
    def async_is_fetching(self, key: str) -> bool:
        """Return True if a refresh for this cache key is in flight."""
        return key in self._tasks

    @callback
    def is_day_fresh(
        self,
        entry_id: str,
        channel: int,
        stream: str,
        date: dt.date,
        include_unlabelled: bool = False,
    ) -> bool:
        """Return True if the cached camera-day needs no refresh."""
        record = self.peek_day(entry_id, channel, stream, date, include_unlabelled)
        if record is None:
            return False
        if record.files and record.schema != FILE_SCHEMA_VERSION:
            # Written by an older version, so it is missing fields this one needs.
            return False
        ttl = self._ttl_for(date, record.error is not None)
        return _age(record.fetched_at) < ttl

    @callback
    def is_calendar_fresh(self, entry_id: str, channel: int, year: int, month: int) -> bool:
        """Return True if the cached month calendar needs no refresh."""
        record = self.peek_calendar(entry_id, channel, year, month)
        if record is None:
            return False
        now = dt_util.now()
        current_month = (year, month) >= (now.year, now.month)
        ttl = TTL_ERROR if record.error else (TTL_TODAY if current_month else TTL_PAST)
        return _age(record.fetched_at) < ttl

    # ----------------------------------------------------------------- fetching

    def _semaphore(self, entry_id: str) -> asyncio.Semaphore:
        """Per-device concurrency gate."""
        if entry_id not in self._semaphores:
            self._semaphores[entry_id] = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        return self._semaphores[entry_id]

    @callback
    def async_ensure_day(
        self,
        entry_id: str,
        channel: int,
        stream: str,
        date: dt.date,
        split_minutes: int,
        *,
        include_unlabelled: bool = False,
        force: bool = False,
    ) -> asyncio.Task[Any] | None:
        """Start a background refresh of a camera-day if it is stale.

        Returns the in-flight task, or None if the cached copy is already fresh.
        Never raises and never blocks: callers serve cached data and let the patch
        arrive later.
        """
        key = day_key(entry_id, channel, stream, date, include_unlabelled)
        if existing := self._tasks.get(key):
            return existing
        if not force and self.is_day_fresh(entry_id, channel, stream, date, include_unlabelled):
            return None

        task = self.hass.async_create_task(
            self._async_fetch_day(
                key, entry_id, channel, stream, date, split_minutes, include_unlabelled
            ),
            name=f"reolink_stamina fetch {key}",
            eager_start=False,
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))
        return task

    @callback
    def _async_search_targets(self, entry_id: str, channel: int) -> list[tuple[str, int]]:
        """Return the devices to ask for this camera, best first.

        Usually one: the camera itself. A camera set up twice -- directly, and as a channel
        on a recorder whose copy the user disabled -- can have its recordings on either, and
        which one is not knowable without asking. Reolink will write to the camera's own card
        and to the recorder, so this is a real question and not a formality.

        Both are asked until one of them answers with recordings, and that one is then
        remembered for the rest of the session. Remembered per camera rather than per day
        because a per-day race would take one day from the recorder and the next from the
        card, and the two describe their recordings differently -- different names, offsets
        and playback ids either side of a midnight boundary.

        A day with nothing on it settles nothing, so no winner is drawn from it and both are
        asked again next time. A camera that never records pays for a second empty search,
        which is the cheapest search there is.
        """
        nominal = (entry_id, channel)
        paired = async_paired_channel(self.hass, entry_id, channel)
        if paired is None:
            # Whatever was learned no longer applies: the recorder's copy of this camera has
            # been taken back into use, so it is not ours to redirect to any more.
            self._sources.pop(nominal, None)
            return [nominal]

        known = self._sources.get(nominal)
        if known is not None and known in (nominal, paired):
            return [known]
        return [nominal, paired]

    async def _async_search_one(
        self,
        entry_id: str,
        channel: int,
        target: tuple[str, int],
        stream: str,
        date: dt.date,
        split_minutes: int,
        include_unlabelled: bool,
    ) -> _SearchOutcome:
        """Ask one device for one camera-day. Never raises, except to be cancelled."""
        target_entry, target_channel = target
        async with self._semaphore(target_entry):
            try:
                files, unlabelled = await async_search_day(
                    self.hass,
                    entry_id,
                    channel,
                    stream,
                    date,
                    split_minutes,
                    include_unlabelled,
                    source_entry_id=target_entry,
                    source_channel=target_channel,
                )
            except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
                return _SearchOutcome(target, error=str(err))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Search failed for %s on %s: %s", entry_id, target, err)
                return _SearchOutcome(target, error=str(err) or type(err).__name__)
        return _SearchOutcome(target, files=files, unlabelled=unlabelled)

    async def _async_fetch_day(
        self,
        key: str,
        entry_id: str,
        channel: int,
        stream: str,
        date: dt.date,
        split_minutes: int,
        include_unlabelled: bool,
    ) -> None:
        """Search one camera-day and update the cache."""
        nominal = (entry_id, channel)
        targets = self._async_search_targets(entry_id, channel)

        # Concurrently, and independently: a paired camera that is offline must not stall or
        # fail the recorder's answer, which is the entire point of asking it.
        outcomes = await asyncio.gather(
            *(
                self._async_search_one(
                    entry_id, channel, target, stream, date, split_minutes, include_unlabelled
                )
                for target in targets
            )
        )

        answered = [outcome for outcome in outcomes if outcome.files]
        if answered:
            # The recorder wins a tie. Continuous recording lives there, so it holds the
            # longer history, and it is the side someone pairing a camera is reaching for.
            chosen = next(
                (outcome for outcome in answered if outcome.target != nominal), answered[0]
            )
            if len(targets) > 1:
                _LOGGER.debug("%s channel %s is served by %s", entry_id, channel, chosen.target)
                self._sources[nominal] = chosen.target
        else:
            usable = [outcome for outcome in outcomes if outcome.error is None]
            if not usable:
                # Every device asked failed. Report the camera's own failure where there is
                # one, since that is the device the user thinks they are looking at.
                own = next(
                    (outcome for outcome in outcomes if outcome.target == nominal), outcomes[0]
                )
                self._record_day_error(key, own.error or "Search failed")
                return
            chosen = next((outcome for outcome in usable if outcome.target == nominal), usable[0])

        self._days[key] = DayRecord(
            files=chosen.files or [],
            fetched_at=time.time(),
            error=None,
            unlabelled=chosen.unlabelled,
            schema=FILE_SCHEMA_VERSION,
        )
        self._schedule_save()
        self._finish(key)

    @callback
    def _record_day_error(self, key: str, message: str) -> None:
        """Attach a failure to a cached day without discarding its data."""
        record = self._days.get(key)
        if record is None:
            self._days[key] = DayRecord(fetched_at=time.time(), error=message)
        else:
            # Keep the stale files: showing yesterday's answer beats showing nothing.
            record.fetched_at = time.time()
            record.error = message
        self._finish(key)

    @callback
    def async_ensure_calendar(
        self,
        entry_id: str,
        channel: int,
        year: int,
        month: int,
        *,
        force: bool = False,
    ) -> asyncio.Task[Any] | None:
        """Start a background refresh of a month's recording calendar if stale."""
        key = calendar_key(entry_id, channel, year, month)
        if existing := self._tasks.get(key):
            return existing
        if not force and self.is_calendar_fresh(entry_id, channel, year, month):
            return None

        task = self.hass.async_create_task(
            self._async_fetch_calendar(key, entry_id, channel, year, month),
            name=f"reolink_stamina calendar {key}",
            eager_start=False,
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))
        return task

    async def _async_fetch_calendar(
        self, key: str, entry_id: str, channel: int, year: int, month: int
    ) -> None:
        """Fetch which days of a month hold recordings."""
        async with self._semaphore(entry_id):
            try:
                days = await async_search_calendar(self.hass, entry_id, channel, year, month)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Calendar search failed for %s: %s", key, err)
                record = self._calendars.get(key) or CalendarRecord()
                record.fetched_at = time.time()
                record.error = str(err) or type(err).__name__
                self._calendars[key] = record
                self._finish(key)
                return

        self._calendars[key] = CalendarRecord(days=days, fetched_at=time.time(), error=None)
        self._schedule_save()
        self._finish(key)

    @callback
    def async_shutdown(self) -> None:
        """Cancel outstanding fetches."""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._listeners.clear()
