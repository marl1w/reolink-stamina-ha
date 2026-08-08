#!/usr/bin/env python3
"""Re-score a journal offline, to choose the constants that were guessed.

Every number in Relevance — how wide a window folds a flapping sensor into one detection, how
much a rate curve is smoothed, how rare an event has to be before it is marked — is applied
when transitions are *read*, never when they are written. That is the whole reason the journal
stores raw state changes: a constant can be changed on a Tuesday and re-applied to every event
ever collected, instead of being baked into a year of somebody's history.

This is the tool that does the re-applying. Point it at a journal and it will say what would
have been marked, so a value can be chosen by looking rather than by guessing.

    scripts/replay.py ~/homeassistant/reolink_stamina_journal.db
    scripts/replay.py journal.db --merge 12 --quantile 0.995
    scripts/replay.py journal.db --sweep merge

It needs no Home Assistant running, only the file. The solar term is skipped, because sunset
depends on a configured location that a copied database does not carry — so the numbers here
are the clock, duration and predecessor terms, which is what the constants under discussion
actually affect.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.reolink_stamina.relevance.events import Event
from custom_components.reolink_stamina.relevance.journal import Transition
from custom_components.reolink_stamina.relevance.rates import build
from custom_components.reolink_stamina.relevance.score import (
    calibrate,
    ready,
    score,
)

_ON = "on"


def read(path: Path) -> list[Transition]:
    """Read every transition out of a journal file."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT camera, entity_id, kind, state, at, context, source "
            "FROM transitions ORDER BY at, id"
        ).fetchall()
    finally:
        connection.close()
    return [Transition(*row) for row in rows]


def derive(transitions: Iterable[Transition], *, window: float, longest: float) -> list[Event]:
    """Fold transitions into events without Home Assistant.

    Deliberately a copy of the shape of `events.derive` rather than a call into it: that one
    needs a running Home Assistant for the local clock and the sun, and the point of this
    script is to run against a file on a laptop. The clock is taken as UTC here, which shifts
    every event by the same amount and so leaves every comparison between constants intact.
    """
    grouped: dict[tuple[str, str], list[Transition]] = {}
    for row in transitions:
        grouped.setdefault((row.camera, row.kind), []).append(row)

    events: list[Event] = []
    for (camera, kind), rows in grouped.items():
        rows.sort(key=lambda item: item.at)
        start: float | None = None
        end: float | None = None
        active: set[str] = set()
        for row in rows:
            if row.state == _ON:
                if start is not None and end is not None and row.at - end > window:
                    events.append(_event(camera, kind, start, end))
                    start = None
                if start is None:
                    start, end = row.at, None
                active.add(row.entity_id)
            else:
                active.discard(row.entity_id)
                if not active and start is not None:
                    end = row.at
            if start is not None and row.at - start >= longest:
                events.append(_event(camera, kind, start, end if end is not None else row.at))
                start, end, active = None, None, set()
        if start is not None:
            events.append(_event(camera, kind, start, end))

    events.sort(key=lambda item: (item.started_at, item.camera, item.kind))
    return events


def _event(camera: str, kind: str, started_at: float, ended_at: float | None) -> Event:
    """Build one event with a UTC clock."""
    moment = datetime.fromtimestamp(started_at, UTC)
    return Event(
        camera=camera,
        kind=kind,
        started_at=started_at,
        ended_at=ended_at,
        duration=None if ended_at is None else round(ended_at - started_at, 3),
        minute_of_day=moment.hour * 60 + moment.minute,
        solar_offset=None,
        solar_phase=None,
        is_weekend=moment.weekday() >= 5,
        day_of_week=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[moment.weekday()],
    )


def run(events: list[Event], *, quantile: float) -> tuple[list[tuple[Event, object]], object]:
    """Build a model, calibrate it, and score everything against itself."""
    now = max((event.started_at for event in events), default=0.0)
    model = build(events, now=now)
    calibrate(model, events, share=quantile)

    marked: list[tuple[Event, object]] = []
    previous: Event | None = None
    for event in events:
        result = score(event, model, previous=previous)
        if result.unusual:
            marked.append((event, result))
        previous = event
    return marked, model


def report(events: list[Event], transitions: int, marked, model, *, window: float) -> None:
    """Print what a person needs to decide whether the constants are right."""
    print(f"\n  transitions {transitions}   events {len(events)}   merge window {window:g}s")
    if events:
        span = (events[-1].started_at - events[0].started_at) / 86400.0
        print(f"  span {span:.1f} days   {len(events) / max(span, 1):.1f} events/day")
    # The ratio that says whether the merge window is right: many more transitions than
    # events means the sensors flap harder than assumed.
    print(f"  transitions per event {transitions / max(len(events), 1):.2f}")

    print("\n  camera                              state          days  events  threshold")
    for camera in sorted(model.per_camera):
        profile = model.per_camera[camera]
        state = "active" if ready(profile) else "collecting"
        threshold = model.thresholds.get(camera)
        cut = f"{threshold:8.2f}" if threshold is not None else "       —"
        print(f"  {camera:<35} {state:<10} {profile.days:6.1f} {profile.events:7d} {cut}")

    share = 100 * len(marked) / max(len(events), 1)
    print(f"\n  marked {len(marked)} of {len(events)} ({share:.2f}%)")
    if events:
        span_weeks = max((events[-1].started_at - events[0].started_at) / 604800.0, 1e-9)
        print(f"  about {len(marked) / span_weeks:.1f} a week")

    if marked:
        print("\n  the ones it would have marked:\n")
        for event, result in marked[-25:]:
            when = datetime.fromtimestamp(event.started_at, UTC).strftime("%Y-%m-%d %H:%M")
            print(f"    {when}  {result.total:6.2f}  {result.reason}")


def main() -> int:
    """Read a journal and report what the current constants would do to it."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("journal", type=Path, help="path to reolink_stamina_journal.db")
    parser.add_argument("--merge", type=float, default=20.0, help="merge window, seconds")
    parser.add_argument("--longest", type=float, default=600.0, help="longest event, seconds")
    parser.add_argument("--quantile", type=float, default=0.99, help="threshold quantile")
    parser.add_argument(
        "--sweep",
        choices=("merge", "quantile"),
        help="try a range of values for one constant and print only the summary of each",
    )
    args = parser.parse_args()

    if not args.journal.exists():
        parser.error(f"no such journal: {args.journal}")

    transitions = read(args.journal)
    if not transitions:
        print("The journal is empty. Nothing to replay yet.")
        return 0

    if args.sweep == "merge":
        print("\n  merge   events  per event   marked   %")
        for window in (5.0, 10.0, 20.0, 30.0, 60.0, 120.0):
            events = derive(transitions, window=window, longest=args.longest)
            marked, _ = run(events, quantile=args.quantile)
            ratio = len(transitions) / max(len(events), 1)
            share = 100 * len(marked) / max(len(events), 1)
            print(f"  {window:5.0f}s {len(events):8d} {ratio:10.2f} {len(marked):8d} {share:6.2f}")
        return 0

    if args.sweep == "quantile":
        events = derive(transitions, window=args.merge, longest=args.longest)
        print("\n  quantile   marked   %      per week")
        span_weeks = max(
            (events[-1].started_at - events[0].started_at) / 604800.0 if events else 1.0, 1e-9
        )
        for quantile in (0.95, 0.98, 0.99, 0.995, 0.999):
            marked, _ = run(events, quantile=quantile)
            share = 100 * len(marked) / max(len(events), 1)
            print(
                f"  {quantile:8.3f} {len(marked):8d} {share:6.2f} {len(marked) / span_weeks:11.1f}"
            )
        return 0

    events = derive(transitions, window=args.merge, longest=args.longest)
    marked, model = run(events, quantile=args.quantile)
    report(events, len(transitions), marked, model, window=args.merge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
