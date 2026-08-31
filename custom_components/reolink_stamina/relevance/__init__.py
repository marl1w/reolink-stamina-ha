"""Relevance: telling the handful of events that matter from the hundreds that don't.

A 24/7 recorder produces the same detections today that it produced yesterday, and the panel
cannot say which three are worth opening. The answer here is to count rather than to
recognise: keep a long record of what each camera normally sees and when, and an event is
interesting when that combination is rare. The cat crossing the drive at 01:00 every night is
common; a person doing it is not, and the difference falls out of arithmetic instead of out
of understanding anything.

This is the first milestone, and it shows the user almost nothing: the journal, the live
subscription that fills it, and the one-off import of whatever history Home Assistant still
holds. Scoring, the badge and the filter chip come next, and they can only be built on
history that was being written down while nobody was looking at it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.start import async_at_started

from ..const import DEFAULT_RELEVANCE_SCOPE, DEFAULT_RELEVANCE_SENSITIVITY, DOMAIN
from .analysis import Analysis
from .backfill import async_backfill, async_backfill_signals
from .journal import Journal
from .watcher import TransitionWatcher

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RelevanceRuntime:
    """What Relevance has running: the journal, the listener, and the model."""

    journal: Journal
    watcher: TransitionWatcher
    analysis: Analysis
    cancel_start: CALLBACK_TYPE | None = None


async def async_start(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    signals: dict[str, list[str]] | None = None,
    sensitivity: str = DEFAULT_RELEVANCE_SENSITIVITY,
    scope: str = DEFAULT_RELEVANCE_SCOPE,
) -> RelevanceRuntime:
    """Open the journal and begin recording.

    Listening is deferred until Home Assistant has finished starting. Setup order between
    integrations is not something this one can insist on, so at the moment it loads the
    Reolink integration may have no entities registered yet — subscribing then would find
    nothing and stay subscribed to nothing until the next reload.

    The import runs as a background task rather than inside setup: it reads up to ninety days
    of history and nothing should wait for it, least of all the sidebar appearing.
    """
    journal = Journal(hass)
    await journal.async_open()
    watcher = TransitionWatcher(hass, journal, signals=signals)
    analysis = Analysis(hass, journal, sensitivity=sensitivity, scope=scope)
    runtime = RelevanceRuntime(journal=journal, watcher=watcher, analysis=analysis)

    async def _begin(_hass: HomeAssistant) -> None:
        watcher.async_start()
        analysis.async_schedule()
        entry.async_create_background_task(
            hass,
            _async_catch_up(hass, journal, analysis, signals=signals or {}),
            name=f"{DOMAIN}_journal_backfill",
        )

    runtime.cancel_start = async_at_started(hass, _begin)
    return runtime


async def _async_catch_up(
    hass: HomeAssistant,
    journal: Journal,
    analysis: Analysis,
    *,
    signals: dict[str, list[str]],
) -> None:
    """Import history if there is any to import, then learn from everything held.

    One background task rather than three, because the order matters: building a model before
    the import lands would learn from a fraction of the history and then wait until three in
    the morning to notice the rest. The signals go between the two for the same reason — they
    are stamped onto transitions, so they have to be stamped before anything counts them.

    Both imports are cheap when there is nothing to do, which is what makes this safe to run
    on every reload. That matters: changing the configuration reloads the entry, and somebody
    who has just chosen a signal should not have to wait until tomorrow to see it.
    """
    await async_backfill(hass, journal)
    await async_backfill_signals(hass, journal, signals)
    await analysis.async_rebuild()


async def async_stop(runtime: RelevanceRuntime) -> None:
    """Stop recording and close the journal, keeping whatever was buffered."""
    if runtime.cancel_start is not None:
        runtime.cancel_start()
        runtime.cancel_start = None
    runtime.analysis.async_cancel()
    runtime.watcher.async_stop()
    await runtime.journal.async_close()


async def async_delete(hass: HomeAssistant) -> None:
    """Delete the journal.

    Called when the integration is removed. What is in there is a record of when a household
    is in and out, and it should not outlive somebody's decision to remove the integration
    that collected it.
    """
    journal = Journal(hass)
    await journal.async_delete()
    _LOGGER.debug("Relevance journal removed")
