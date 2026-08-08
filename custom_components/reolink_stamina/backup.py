"""Making the journal safe to copy while Home Assistant is running.

Backups archive the whole configuration directory, so the journal is included wherever it
sits — that part needs no arranging. What does need arranging is that it be a *usable* copy.
The database runs in write-ahead mode, and the exclusion list drops `*.db-shm` while keeping
`*.db-wal`, so an archive taken with no warning holds a database file and a separate log read
some moments apart, with no guarantee the two belong together.

One checkpoint before the archive starts folds the log into the database and empties it,
which leaves a single self-contained file to copy.

Unlike the recorder, which locks its database and fails the backup if it cannot, nothing here
is allowed to stop a backup. A household's configuration is worth more than this integration's
statistics, and the worst case without the checkpoint is a journal that loses its most recent
transitions when restored — not a backup that did not happen.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Leave the journal as one consistent file before the archive is taken."""
    data = hass.data.get(DOMAIN)
    runtime = getattr(data, "relevance", None)
    if runtime is None:
        # Relevance is off, so there is no journal to make consistent.
        return

    try:
        await runtime.journal.async_checkpoint()
    except Exception:
        _LOGGER.warning(
            "Could not checkpoint the Relevance journal before the backup; it may restore "
            "without its most recent detections",
            exc_info=True,
        )


async def async_post_backup(hass: HomeAssistant) -> None:
    """Nothing was held, so nothing has to be released."""
