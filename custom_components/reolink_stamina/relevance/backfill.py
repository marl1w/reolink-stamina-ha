"""Importing whatever history the recorder still holds.

Switching Relevance on starts the journal from that instant, which means a fortnight of
waiting before anything can be scored. Home Assistant has usually been recording these same
sensors all along, though, so the wait can be shortened by however much it kept — and this
is the one place the recorder is read at all. After this runs, the journal is fed entirely
by the live watcher and the recorder's retention stops mattering.

Two things this deliberately does not do.

It does not assume ten days. `purge_keep_days` defaults to ten and is frequently changed;
somebody who set ninety should get ninety, and asking is no harder than guessing. It is
bounded above all the same, because a first import is not the place to walk years of history.

It does not run in one query. A single call spanning ninety days across every detection
sensor on every camera is a heavy read of a database that is also serving the rest of Home
Assistant, and a restart must never feel slow because of a feature that has not shown the
user anything yet. So it goes a day at a time, in the background, yielding between chunks —
and because the journal ignores transitions it already holds, an import that is interrupted
half way simply resumes without duplicating anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    JOURNAL_BACKFILL_CHUNK_HOURS,
    JOURNAL_BACKFILL_DAYS_DEFAULT,
    JOURNAL_BACKFILL_DAYS_MAX,
    JOURNAL_META_BACKFILLED,
    JOURNAL_META_SIGNALS,
    JOURNAL_SOURCE_BACKFILL,
)
from .journal import Journal, Transition
from .watcher import async_detection_map, async_signal_map

_LOGGER = logging.getLogger(__name__)


def async_retention_days(hass: HomeAssistant) -> int:
    """Return how many days of history to ask the recorder for.

    The recorder knows what it keeps, so it is asked. Every way of failing to find out —
    no recorder, a version that stopped exposing it, a nonsense value — lands on the
    documented default, because an import that reaches less far than it could is a much
    smaller problem than one that refuses to run.
    """
    try:
        from homeassistant.components.recorder import get_instance

        instance = get_instance(hass)
    except (ImportError, KeyError, RuntimeError):
        return JOURNAL_BACKFILL_DAYS_DEFAULT

    keep_days = getattr(instance, "keep_days", None)
    try:
        days = int(keep_days)
    except (TypeError, ValueError):
        return JOURNAL_BACKFILL_DAYS_DEFAULT
    if days <= 0:
        return JOURNAL_BACKFILL_DAYS_DEFAULT
    return min(days, JOURNAL_BACKFILL_DAYS_MAX)


async def _async_history(
    hass: HomeAssistant, start: dt.datetime, end: dt.datetime, entity_ids: list[str]
) -> dict[str, list[Any]]:
    """Read one window of state history, or nothing at all if the recorder cannot answer.

    Deliberately the same call the panel's own detection lookup makes, with the same
    arguments: two readers of the same table disagreeing about what counts as a state change
    is a bug that would only ever show up as counts that do not add up.
    """
    try:
        from homeassistant.components.recorder import get_instance, history
    except ImportError:
        return {}

    try:
        instance = get_instance(hass)
    except (KeyError, RuntimeError):
        return {}
    if instance is None:
        return {}

    def _read() -> dict[str, list[Any]]:
        return history.get_significant_states(
            hass,
            start,
            end,
            entity_ids,
            None,
            True,
            True,
            False,
            True,
        )

    try:
        return await instance.async_add_executor_job(_read)
    except Exception:
        _LOGGER.debug("Could not read history for the journal", exc_info=True)
        return {}


async def async_backfill(hass: HomeAssistant, journal: Journal) -> int:
    """Import the recorder's detection history into the journal, once.

    Returns how many transitions were new. Running it again is harmless — every row is
    ignored on the second pass — but the marker means a restart does not pay for it.
    """
    if await journal.async_get_meta(JOURNAL_META_BACKFILLED) is not None:
        return 0

    entities = async_detection_map(hass)
    if not entities:
        # Nothing to import, and nothing to remember either: the recorders may simply not be
        # loaded yet, and marking this done would lose the history for good.
        _LOGGER.debug("No detection sensors to import history for; will try again next reload")
        return 0

    days = async_retention_days(hass)
    now = dt_util.utcnow()
    start = now - dt.timedelta(days=days)
    chunk = dt.timedelta(hours=JOURNAL_BACKFILL_CHUNK_HOURS)
    entity_ids = list(entities)

    _LOGGER.debug("Importing %s days of history for %s sensors", days, len(entity_ids))
    imported = 0
    window_start = start
    while window_start < now:
        window_end = min(window_start + chunk, now)
        states = await _async_history(hass, window_start, window_end, entity_ids)

        rows: list[Transition] = []
        for entity_id, history_states in (states or {}).items():
            known = entities.get(entity_id)
            if known is None:
                continue
            camera, kind = known
            for state in history_states:
                changed = getattr(state, "last_changed", None)
                if changed is None:
                    continue
                rows.append(
                    Transition(
                        camera=camera,
                        entity_id=entity_id,
                        kind=kind,
                        state=state.state,
                        at=changed.timestamp(),
                        source=JOURNAL_SOURCE_BACKFILL,
                    )
                )

        imported += await journal.async_add(rows)
        window_start = window_end
        # Between chunks, not inside them: the read itself is already on the recorder's
        # executor, and this is what keeps a ninety-day import from monopolising the loop.
        await asyncio.sleep(0)

    await journal.async_set_meta(JOURNAL_META_BACKFILLED, now.isoformat())
    _LOGGER.info(
        "Imported %s detection transitions from %s days of Home Assistant history", imported, days
    )
    return imported


def _state_at(timeline: list[tuple[float, str]], at: float) -> str:
    """Return what an entity read at a moment, from its history.

    The last state that had already begun. Before the history starts there is nothing to know,
    and `unknown` is what the live watcher writes for an entity it cannot see — so an event
    older than the recorder's retention says the same thing either way.
    """
    low, high = 0, len(timeline)
    while low < high:
        middle = (low + high) // 2
        if timeline[middle][0] <= at:
            low = middle + 1
        else:
            high = middle
    return timeline[low - 1][1] if low else STATE_UNKNOWN


async def async_backfill_signals(
    hass: HomeAssistant,
    journal: Journal,
    signals: dict[str, list[str]],
) -> int:
    """Reconstruct what the configured signals said, and stamp it onto history.

    Signals are snapshotted when a transition is written, which is right for everything the
    live watcher sees and useless for everything that happened first. Somebody who adds "is
    anybody home" after a month of collecting would otherwise be told to wait another week
    before it counted for anything — while Home Assistant has the answer for that whole month
    sitting in the recorder.

    Runs when the set of signals changes, and does nothing when it has not: the marker records
    which entities were stamped, so a reload is free and adding one signal re-reads history
    for all of them, which is the only way the snapshots stay consistent with each other.

    Returns how many transitions were stamped.
    """
    # The same map the live watcher uses, so a reconstructed snapshot and a recorded one hold
    # the same entities. Two readers disagreeing here would surface only as counts that do
    # not add up, months later.
    wanted = async_signal_map(hass, signals)
    # Keyed on the signals themselves rather than on "done": adding one has to re-stamp
    # everything, because a snapshot missing an entity is not the same as one recording it
    # as absent, and the two must not end up mixed together in one history.
    fingerprint = json.dumps({camera: sorted(ids) for camera, ids in sorted(wanted.items())})
    if await journal.async_get_meta(JOURNAL_META_SIGNALS) == fingerprint:
        return 0

    if not wanted:
        # Every signal removed. Recording that is what stops the next reload doing this again.
        await journal.async_set_meta(JOURNAL_META_SIGNALS, fingerprint)
        return 0

    # Read the rows first, because they define the window to ask the recorder about. The
    # nightly rebuild already holds every transition at once, so this is a footprint the
    # feature is paying anyway rather than a new one.
    held = {camera: await journal.async_transitions(camera=camera) for camera in wanted}
    moments = [row.at for rows in held.values() for row in rows]
    if not moments:
        return 0

    entity_ids = sorted({entity_id for entities in wanted.values() for entity_id in entities})
    start = dt_util.utc_from_timestamp(min(moments))
    end = dt_util.utc_from_timestamp(max(moments))
    states = await _async_history(hass, start, end, entity_ids)

    timelines: dict[str, list[tuple[float, str]]] = {}
    for entity_id in entity_ids:
        found = []
        for state in states.get(entity_id) or ():
            changed = getattr(state, "last_changed", None)
            if changed is not None:
                found.append((changed.timestamp(), state.state))
        found.sort()
        timelines[entity_id] = found

    stamps: list[tuple[str, float, str]] = []
    for camera, entities in wanted.items():
        for row in held[camera]:
            snapshot = {
                entity_id: _state_at(timelines.get(entity_id) or [], row.at)
                for entity_id in entities
            }
            stamps.append((row.entity_id, row.at, json.dumps(snapshot, separators=(",", ":"))))
        # Between cameras: the rows are already in memory, but the writes are not free and a
        # reload should not be felt.
        await asyncio.sleep(0)

    stamped = await journal.async_stamp_context(stamps)
    await journal.async_set_meta(JOURNAL_META_SIGNALS, fingerprint)
    _LOGGER.info(
        "Reconstructed %s signals across %s transitions of history", len(entity_ids), stamped
    )
    return stamped
