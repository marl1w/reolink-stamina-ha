"""The journal has to survive a backup, and must never be the reason one fails.

Two separate claims. The journal is inside the configuration directory, which is what Home
Assistant archives — so it is included without anything being arranged. And the copy has to
be usable: the exclusion list drops `*.db-shm` but keeps `*.db-wal`, so a database and its
write-ahead log would otherwise be archived as two files read moments apart.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from homeassistant.components.backup.const import (
    EXCLUDE_DATABASE_FROM_BACKUP,
    EXCLUDE_FROM_BACKUP,
)
from homeassistant.core import HomeAssistant
import pytest

from custom_components.reolink_stamina.backup import async_post_backup, async_pre_backup
from custom_components.reolink_stamina.const import DOMAIN, JOURNAL_FILENAME
from custom_components.reolink_stamina.relevance.journal import Journal, Transition


@pytest.fixture
async def journal(hass, tmp_path):
    """Return an open journal holding one transition."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    await store.async_add(
        [
            Transition(
                camera="entry1:0",
                entity_id="binary_sensor.drive_person",
                kind="person",
                state="on",
                at=1000.0,
            )
        ]
    )
    yield store
    await store.async_close()


def test_the_journal_is_not_on_any_exclusion_list():
    """It lives in the configuration directory, which is what a backup archives.

    A guard rather than a tautology: if Home Assistant ever excludes `*.db` wholesale, the
    journal stops being backed up and nothing else would say so.
    """
    excluded = [*EXCLUDE_FROM_BACKUP, *EXCLUDE_DATABASE_FROM_BACKUP]

    assert not any(Path(JOURNAL_FILENAME).match(pattern) for pattern in excluded)
    # The write-ahead log rides along with it, and the scratch file is excluded and
    # recreatable — which is exactly why the checkpoint below matters.
    assert not any(Path(f"{JOURNAL_FILENAME}-wal").match(pattern) for pattern in excluded)


async def test_a_backup_leaves_the_log_empty(hass: HomeAssistant, journal, tmp_path):
    """One self-contained file to copy, rather than a database and a log to reconcile."""
    hass.data[DOMAIN] = type("Data", (), {"relevance": type("R", (), {"journal": journal})})()

    await async_pre_backup(hass)

    wal = tmp_path / "journal.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


async def test_buffered_transitions_are_written_before_a_backup(hass, journal, tmp_path):
    """Otherwise the archive misses whatever was still in memory when it started."""
    journal.async_record(
        Transition(
            camera="entry1:0",
            entity_id="binary_sensor.drive_person",
            kind="person",
            state="off",
            at=1010.0,
        )
    )
    hass.data[DOMAIN] = type("Data", (), {"relevance": type("R", (), {"journal": journal})})()

    await async_pre_backup(hass)

    assert (await journal.async_coverage())["transitions"] == 2


async def test_nothing_happens_when_the_feature_is_off(hass: HomeAssistant):
    """No journal, nothing to make consistent, and no reason to complain about it."""
    await async_pre_backup(hass)
    await async_post_backup(hass)


async def test_a_failed_checkpoint_does_not_stop_the_backup(hass, journal, caplog):
    """A household's configuration is worth more than this integration's statistics."""
    hass.data[DOMAIN] = type("Data", (), {"relevance": type("R", (), {"journal": journal})})()

    with patch.object(journal, "async_checkpoint", side_effect=OSError("disk gone")):
        await async_pre_backup(hass)

    assert "checkpoint" in caplog.text
