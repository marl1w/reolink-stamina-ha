"""The journal: what it stores, what it refuses to store twice, and what it reports.

The duplicate rule is the one worth having tests for. Rate estimates are counts, so a
transition arriving twice is not a tidiness problem — it is a wrong answer, and the two
paths that write to the journal overlap on purpose: the import re-reads the state that was
current when each chunk opened, and re-running it re-reads everything.
"""

from __future__ import annotations

import sqlite3

import pytest

from custom_components.reolink_stamina.const import JOURNAL_SOURCE_BACKFILL
from custom_components.reolink_stamina.relevance.journal import (
    Journal,
    Transition,
    camera_key,
)


@pytest.fixture
async def journal(hass, tmp_path):
    """Return an open journal in a temporary file."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    yield store
    await store.async_close()


def _transition(at: float, *, state: str = "on", entity: str = "binary_sensor.drive_person"):
    return Transition(
        camera="entry1:0",
        entity_id=entity,
        kind="person",
        state=state,
        at=at,
        source=JOURNAL_SOURCE_BACKFILL,
    )


def test_camera_key_is_built_from_ids_that_do_not_move():
    """A rename must not split one camera's history in two."""
    assert camera_key("01JABC", 3) == "01JABC:3"


async def test_open_creates_the_schema(journal, tmp_path):
    """A fresh file gets its tables and records the version it was built at."""
    assert (tmp_path / "journal.db").exists()
    assert await journal.async_get_meta("schema_version") == "1"


async def test_reopening_an_existing_file_is_harmless(hass, tmp_path):
    """Migrations are replayed on every open, so they have to be idempotent."""
    first = Journal(hass, tmp_path / "journal.db")
    await first.async_open()
    assert await first.async_add([_transition(1000.0)]) == 1
    await first.async_close()

    second = Journal(hass, tmp_path / "journal.db")
    await second.async_open()
    coverage = await second.async_coverage()
    await second.async_close()

    assert coverage["transitions"] == 1


async def test_add_returns_how_many_were_new(journal):
    """The import path needs to know what it actually contributed."""
    assert await journal.async_add([_transition(10.0), _transition(20.0, state="off")]) == 2


async def test_the_same_moment_is_never_stored_twice(journal):
    """One sensor cannot change twice at one instant, so a repeat is the same fact again."""
    await journal.async_add([_transition(10.0)])
    added = await journal.async_add([_transition(10.0), _transition(11.0, state="off")])

    assert added == 1
    coverage = await journal.async_coverage()
    assert coverage["transitions"] == 2


async def test_two_sensors_may_share_a_moment(journal):
    """Several sensors fire together on one arrival; that is a real thing, not a duplicate."""
    added = await journal.async_add(
        [
            _transition(10.0, entity="binary_sensor.drive_person"),
            _transition(10.0, entity="binary_sensor.drive_motion"),
        ]
    )
    assert added == 2


async def test_recording_buffers_until_flushed(journal):
    """Transitions are not committed one at a time; a flush is what writes them."""
    journal.async_record(_transition(30.0))
    assert (await journal.async_coverage())["transitions"] == 0

    await journal.async_flush()
    assert (await journal.async_coverage())["transitions"] == 1


async def test_closing_flushes_what_was_buffered(hass, tmp_path):
    """A reload must not lose the last few seconds it was holding."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    store.async_record(_transition(40.0))
    await store.async_close()

    again = Journal(hass, tmp_path / "journal.db")
    await again.async_open()
    coverage = await again.async_coverage()
    await again.async_close()

    assert coverage["transitions"] == 1


async def test_a_failed_write_is_dropped_rather_than_retried(journal, caplog):
    """A buffer that keeps failed rows grows without bound; a logged gap does not."""
    journal.async_record(_transition(50.0))
    # Simulating a database that has gone away underneath a buffered write.
    journal._connection.close()

    await journal.async_flush()

    assert "lost" in caplog.text


async def test_coverage_reports_each_camera_separately(journal):
    """The panel's collecting state is per camera, so the numbers behind it must be too."""
    await journal.async_add(
        [
            _transition(0.0),
            _transition(86400.0, state="off"),
            Transition(
                camera="entry1:1",
                entity_id="binary_sensor.gate_vehicle",
                kind="vehicle",
                state="on",
                at=100.0,
            ),
        ]
    )

    cameras = {row["camera"]: row for row in (await journal.async_coverage())["cameras"]}

    assert cameras["entry1:0"]["transitions"] == 2
    assert cameras["entry1:0"]["days"] == 1.0
    assert cameras["entry1:1"]["kinds"] == 1


async def test_purge_removes_only_what_is_older(journal):
    """Retention is the user's to set, and it must not take the recent history with it."""
    await journal.async_add([_transition(100.0), _transition(200.0, state="off")])

    assert await journal.async_purge(150.0) == 1
    assert (await journal.async_coverage())["transitions"] == 1


async def test_delete_removes_the_file(hass, tmp_path):
    """Removing the integration must not leave a record of the household behind."""
    path = tmp_path / "journal.db"
    store = Journal(hass, path)
    await store.async_open()
    await store.async_add([_transition(10.0)])
    await store.async_delete()

    assert not path.exists()


async def test_a_closed_journal_accepts_nothing(hass, tmp_path):
    """Writing after close is a bug elsewhere; it must not raise here."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    await store.async_close()

    assert await store.async_add([_transition(10.0)]) == 0
    assert await store.async_get_meta("schema_version") is None
    assert (await store.async_coverage())["open"] is False


async def test_the_unique_index_exists_under_its_own_name(hass, tmp_path):
    """Named so a later migration can find it; renaming it silently would break dedup."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    connection = sqlite3.connect(tmp_path / "journal.db")
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    connection.close()
    await store.async_close()

    assert "transitions_moment" in names
