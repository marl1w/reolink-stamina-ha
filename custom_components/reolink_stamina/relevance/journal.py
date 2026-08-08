"""The long record of what the cameras have seen.

Relevance works by counting: an event is interesting when this camera has rarely seen
anything like it at this hour. That needs months of history, and nothing in the stack keeps
months of it. Home Assistant's recorder purges after ten days by default, and the Reolink
search API reaches about thirty — so history that is not written down as it happens cannot
be recovered later at any price. This module is that written record, and it is the first
thing to exist because every day it is not running is a day that can never be scored.

Two decisions are worth stating, because everything else follows from them.

**Store raw, interpret at read time.** What lands here is every state change of every
detection sensor, verbatim, with the signal states of the moment beside it. Nothing is
merged, bucketed, deduplicated or judged on the way in. Sensors flap — a Reolink binary
sensor can go on, off and on again inside two seconds — and how wide a window folds those
into one event is a constant nobody can pick correctly without first looking at real data.
Deciding it here would bake a guess into a year of somebody's history; deciding it when
`events` is derived leaves it a number that can be changed on a Tuesday and re-applied to
everything already collected. The same argument covers signal normalisation and the
threshold, which is why none of them appear in this file.

**Nothing is journalled until the user asks.** This is a behavioural record of a household —
when people come and go, and when nobody is in — and keeping one quietly because somebody
installed a video panel is not acceptable, however small and local the file is. So the
feature is off, the file does not exist, and removing the integration deletes it.

SQLite because `sqlite3` is in the standard library, which keeps the promise that this
integration installs with no dependencies. Its own file, never the recorder's: that database
belongs to the recorder, and this one has to outlive its retention to be worth having.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from ..const import (
    JOURNAL_FILENAME,
    JOURNAL_FLUSH_ROWS,
    JOURNAL_FLUSH_SECONDS,
    JOURNAL_META_SCHEMA,
    JOURNAL_SOURCE_LIVE,
)

_LOGGER = logging.getLogger(__name__)


# Every migration ever applied, in order, each guarded so that replaying it on a database
# that already has it is harmless. A file records the highest version it has seen; opening it
# replays everything above that number. Entries are never reordered and never edited once
# released, because somebody's year of history is brought forward by exactly this list.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS transitions (
                id        INTEGER PRIMARY KEY,
                camera    TEXT    NOT NULL,
                entity_id TEXT    NOT NULL,
                kind      TEXT    NOT NULL,
                state     TEXT    NOT NULL,
                at        REAL    NOT NULL,
                context   TEXT,
                source    TEXT    NOT NULL
            )
            """,
            # The one piece of interpretation allowed on the way in, and it is not a
            # judgement: a sensor cannot change twice at the same instant, so a repeat of
            # (entity, moment) is the same fact arriving twice. That happens constantly —
            # every backfill chunk re-reads the state that was current when it opened, and a
            # re-run of the import re-reads all of them — and letting it through would
            # inflate exactly the counts this file exists to keep honest.
            """
            CREATE UNIQUE INDEX IF NOT EXISTS transitions_moment
                ON transitions (entity_id, at)
            """,
            # Every read is "this camera, over this window", so this is the index that makes
            # the nightly rate tables cheap to build.
            """
            CREATE INDEX IF NOT EXISTS transitions_camera_at
                ON transitions (camera, at)
            """,
        ),
    ),
)


def camera_key(entry_id: str, channel: int) -> str:
    """Return the journal's stable identity for one camera.

    A config entry id and a channel, because that pair is what the panel already addresses
    cameras by and neither half moves when somebody renames a camera or re-points it. A
    friendly name would have been readable and would have quietly split one camera's history
    in two the first time it was renamed.
    """
    return f"{entry_id}:{channel}"


@dataclass(slots=True, frozen=True)
class Transition:
    """One state change of one detection sensor, exactly as it happened."""

    camera: str
    entity_id: str
    kind: str
    state: str
    # Unix seconds, UTC. From Home Assistant's own clock rather than the recorder's, which is
    # what lets two NVRs be compared: their clocks disagree, and this one does not.
    at: float
    # A JSON snapshot of the configured signals at this instant, or None while no signals are
    # configured. Raw states, never bucketed — normalisation is config applied when scoring,
    # so changing a bucketing re-scores the history rather than losing it.
    context: str | None = None
    source: str = JOURNAL_SOURCE_LIVE


class Journal:
    """The transitions database, and the only thing that writes to it.

    All access is serialised through one lock and one connection: SQLite handles concurrent
    readers happily and concurrent writers badly, and there is nothing here worth the
    complexity of a pool. Everything touching the file runs in the executor, because a commit
    is a disk write and the event loop must not wait for one.
    """

    def __init__(self, hass: HomeAssistant, path: str | Path | None = None) -> None:
        """Prepare a journal, without opening it."""
        self._hass = hass
        self._path = Path(path) if path is not None else Path(hass.config.path(JOURNAL_FILENAME))
        self._connection: sqlite3.Connection | None = None
        self._pending: list[Transition] = []
        self._lock = asyncio.Lock()
        self._cancel_flush: CALLBACK_TYPE | None = None

    @property
    def path(self) -> Path:
        """Return where the database lives."""
        return self._path

    # ---------------------------------------------------------------- lifecycle

    async def async_open(self) -> None:
        """Open the database, creating or migrating the schema as needed."""
        await self._hass.async_add_executor_job(self._open)

    def _open(self) -> None:
        """Connect and migrate. Runs in the executor."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False)
        # WAL so a long read cannot block the next transition from being written, and NORMAL
        # because the alternative is an fsync per commit to protect a statistical record
        # against losing its last few seconds.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            self._migrate(connection)
        except sqlite3.Error:
            # A half-migrated file is bad enough without also leaking the handle to it, and
            # leaving `_connection` unset is what makes every later call a no-op rather than
            # a crash.
            connection.close()
            raise
        self._connection = connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Bring the file up to the current schema. Runs in the executor."""
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (JOURNAL_META_SCHEMA,)
        ).fetchone()
        current = int(row[0]) if row is not None else 0

        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (JOURNAL_META_SCHEMA, str(version)),
            )
            _LOGGER.debug("Journal migrated to schema %s", version)
        connection.commit()

    async def async_close(self) -> None:
        """Flush anything buffered and close the database."""
        await self.async_flush()
        async with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            await self._hass.async_add_executor_job(connection.close)

    async def async_delete(self) -> None:
        """Remove the database entirely, including its write-ahead files.

        Called when the integration is removed. A behavioural record of a household should
        not survive somebody deciding they no longer want the integration that kept it.
        """
        await self.async_close()

        def _unlink() -> None:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)

        await self._hass.async_add_executor_job(_unlink)

    # ---------------------------------------------------------------- writing

    @callback
    def async_record(self, transition: Transition) -> None:
        """Buffer one transition, to be written with the next flush.

        A callback rather than a coroutine because it is called from a state-change listener,
        which must not wait for a disk write to return.
        """
        self._pending.append(transition)
        if len(self._pending) >= JOURNAL_FLUSH_ROWS:
            self._hass.async_create_task(self.async_flush())
        elif self._cancel_flush is None:
            self._cancel_flush = async_call_later(
                self._hass, JOURNAL_FLUSH_SECONDS, self._async_flush_due
            )

    @callback
    def _async_flush_due(self, _now: Any) -> None:
        """Write the buffer out because the timer expired."""
        self._cancel_flush = None
        self._hass.async_create_task(self.async_flush())

    async def async_flush(self) -> None:
        """Write everything buffered so far."""
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None
        async with self._lock:
            pending, self._pending = self._pending, []
            if not pending or self._connection is None:
                return
            try:
                await self._hass.async_add_executor_job(self._insert, pending)
            except sqlite3.Error:
                # Dropped rather than retried: a buffer that keeps failed rows grows without
                # bound, and the thing being protected is a statistical record that a gap
                # barely dents. The log line is what turns a silent gap into a report.
                _LOGGER.warning(
                    "Could not write %s transitions to the journal; they are lost",
                    len(pending),
                    exc_info=True,
                )

    async def async_add(self, transitions: list[Transition]) -> int:
        """Write transitions straight through, returning how many were new.

        The import path. It bypasses the buffer because it already arrives in day-sized
        batches, and because it needs to know how much it actually added.
        """
        if not transitions:
            return 0
        async with self._lock:
            if self._connection is None:
                return 0
            return await self._hass.async_add_executor_job(self._insert, transitions)

    def _insert(self, transitions: list[Transition]) -> int:
        """Insert transitions, ignoring ones already held. Runs in the executor."""
        assert self._connection is not None
        cursor = self._connection.executemany(
            "INSERT OR IGNORE INTO transitions "
            "(camera, entity_id, kind, state, at, context, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.camera,
                    item.entity_id,
                    item.kind,
                    item.state,
                    item.at,
                    item.context,
                    item.source,
                )
                for item in transitions
            ],
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    async def async_stamp_context(self, stamps: list[tuple[str, float, str]]) -> int:
        """Attach signal snapshots to transitions already written, returning how many changed.

        The one place a row is rewritten rather than appended, and it exists because signals
        are snapshotted at write time. Without this, adding a signal means it says nothing
        about anything until enough new detections accumulate to learn from — a week at best,
        on a camera nobody walks past. Home Assistant has been recording those same entities
        all along, so the honest thing is to reconstruct what they said and stamp it on.

        Still not interpretation: what gets written is the raw state the recorder holds for
        that instant, exactly what the live watcher would have written had the signal been
        configured at the time.
        """
        if not stamps:
            return 0
        async with self._lock:
            if self._connection is None:
                return 0
            return await self._hass.async_add_executor_job(self._stamp, stamps)

    def _stamp(self, stamps: list[tuple[str, float, str]]) -> int:
        """Rewrite the context of known rows. Runs in the executor."""
        assert self._connection is not None
        cursor = self._connection.executemany(
            "UPDATE transitions SET context = ? WHERE entity_id = ? AND at = ?",
            [(context, entity_id, at) for entity_id, at, context in stamps],
        )
        self._connection.commit()
        return int(cursor.rowcount or 0)

    async def async_checkpoint(self) -> None:
        """Fold the write-ahead log back into the database file.

        For backups. Home Assistant archives the whole configuration directory, and it
        excludes `*.db-shm` but not `*.db-wal` — so without this the archive would hold a
        database and a separate write-ahead log read a moment apart, which is not guaranteed
        to be a pair. Checkpointing in TRUNCATE mode empties the log into the file, leaving
        one self-contained database to copy and an empty log beside it.
        """
        await self.async_flush()
        async with self._lock:
            if self._connection is None:
                return
            await self._hass.async_add_executor_job(self._checkpoint)

    def _checkpoint(self) -> None:
        """Truncate the write-ahead log. Runs in the executor."""
        assert self._connection is not None
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def async_purge(self, before: float) -> int:
        """Delete transitions older than a moment, returning how many went."""
        async with self._lock:
            if self._connection is None:
                return 0
            return await self._hass.async_add_executor_job(self._purge, before)

    def _purge(self, before: float) -> int:
        """Delete and reclaim. Runs in the executor."""
        assert self._connection is not None
        cursor = self._connection.execute("DELETE FROM transitions WHERE at < ?", (before,))
        self._connection.commit()
        return int(cursor.rowcount or 0)

    # ---------------------------------------------------------------- metadata

    async def async_get_meta(self, key: str) -> str | None:
        """Read one metadata value, or None if it was never set."""
        async with self._lock:
            if self._connection is None:
                return None
            return await self._hass.async_add_executor_job(self._get_meta, key)

    def _get_meta(self, key: str) -> str | None:
        """Read metadata. Runs in the executor."""
        assert self._connection is not None
        row = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    async def async_set_meta(self, key: str, value: str) -> None:
        """Write one metadata value."""
        async with self._lock:
            if self._connection is None:
                return
            await self._hass.async_add_executor_job(self._set_meta, key, value)

    def _set_meta(self, key: str, value: str) -> None:
        """Write metadata. Runs in the executor."""
        assert self._connection is not None
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._connection.commit()

    # ---------------------------------------------------------------- reading

    async def async_transitions(
        self,
        *,
        camera: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[Transition]:
        """Return transitions in time order, oldest first.

        Everything downstream reads through here, so nothing else ever sees SQL. Whole
        history is the normal case rather than the exception: the rate tables are rebuilt
        from scratch each night, which is what lets a constant change and take effect on
        everything already collected.
        """
        await self.async_flush()
        async with self._lock:
            if self._connection is None:
                return []
            return await self._hass.async_add_executor_job(self._select, camera, since, until)

    def _select(
        self, camera: str | None, since: float | None, until: float | None
    ) -> list[Transition]:
        """Read transitions. Runs in the executor."""
        assert self._connection is not None
        clauses: list[str] = []
        values: list[Any] = []
        if camera is not None:
            clauses.append("camera = ?")
            values.append(camera)
        if since is not None:
            clauses.append("at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("at < ?")
            values.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        return [
            Transition(
                camera=row[0],
                entity_id=row[1],
                kind=row[2],
                state=row[3],
                at=row[4],
                context=row[5],
                source=row[6],
            )
            for row in self._connection.execute(
                "SELECT camera, entity_id, kind, state, at, context, source "
                f"FROM transitions{where} ORDER BY at, id",
                values,
            )
        ]

    async def async_coverage(self) -> dict[str, Any]:
        """Return what the journal holds, per camera and overall.

        This is what diagnostics reports and what the panel's "still collecting" state will
        be built from, so it answers the question both of them ask: how long has this camera
        been watched, and has anything actually been seen.

        Cameras appear as `entry_id:channel`, which names no camera — the same rule the rest
        of diagnostics keeps, and the id is what ties a line here to the Reolink integration.
        """
        async with self._lock:
            if self._connection is None:
                return {"open": False, "cameras": [], "transitions": 0}
            return await self._hass.async_add_executor_job(self._coverage)

    def _coverage(self) -> dict[str, Any]:
        """Summarise the database. Runs in the executor."""
        assert self._connection is not None
        now = time.time()
        cameras = [
            {
                "camera": camera,
                "transitions": int(count),
                "kinds": int(kinds),
                "first": first,
                "last": last,
                # `is not None` rather than a truthiness test: a moment is a number, and a
                # camera whose first transition sits at zero has a span like any other.
                "days": (
                    round((last - first) / 86400.0, 2)
                    if first is not None and last is not None
                    else 0.0
                ),
            }
            for camera, count, kinds, first, last in self._connection.execute(
                "SELECT camera, COUNT(*), COUNT(DISTINCT kind), MIN(at), MAX(at) "
                "FROM transitions GROUP BY camera ORDER BY camera"
            )
        ]
        total = int(self._connection.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] or 0)
        size = 0
        for suffix in ("", "-wal"):
            path = Path(f"{self._path}{suffix}")
            if path.exists():
                size += path.stat().st_size

        return {
            "open": True,
            "transitions": total,
            "bytes": size,
            "schema_version": self._get_meta(JOURNAL_META_SCHEMA),
            "now": now,
            "cameras": cameras,
        }
