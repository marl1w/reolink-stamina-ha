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
    scripts/replay.py journal.db --sweep scope

It needs no Home Assistant running, only the file. The solar term is skipped, because sunset
depends on a configured location that a copied database does not carry — so the numbers here
are the clock, duration and predecessor terms, which is what the constants under discussion
actually affect.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import datetime as dt
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homeassistant.util import dt as dt_util

from custom_components.reolink_stamina.const import (
    DEFAULT_RELEVANCE_SCOPE,
    DEFAULT_RELEVANCE_SENSITIVITY,
    EVENT_MAX_SECONDS,
    EVENT_MERGE_SECONDS,
    RELEVANCE_SCOPES,
    RELEVANCE_SENSITIVITY_FLOORS,
    SCORE_QUANTILE,
)
from custom_components.reolink_stamina.relevance.events import Event, derive as events_derive
from custom_components.reolink_stamina.relevance.journal import Transition
from custom_components.reolink_stamina.relevance.rates import build, preceded
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


class _NoSun:
    """A solar clock for a journal that arrived without a location.

    A copied database carries timestamps and nothing else — no latitude, no longitude — so
    where the sun was cannot be recovered and is reported as unknown rather than guessed.
    `derive` treats that exactly as it treats a Home Assistant with no location set, and the
    solar term simply does not appear.
    """

    def offset(self, _moment: dt.datetime) -> int | None:
        """Return no offset, because there is no sunset to measure from."""
        return None

    def phase(self, _moment: dt.datetime) -> str | None:
        """Return no phase, for the same reason."""
        return None


def derive(transitions: Iterable[Transition], *, window: float, longest: float) -> list[Event]:
    """Fold transitions into events, through the code the panel itself uses.

    This was a copy of `events.derive` once, on the reasoning that the real one needs a
    running Home Assistant. It does not — it needs a clock and a sun, and both can be handed
    to it — and the copy quietly drifted from the original in two ways that matter.

    It read the clock as **UTC**. Every clock-based number then sat an offset away from what
    the panel would say: asking this tool whether a person at two in the morning would be
    marked actually asked about four in the morning in Rome. Comparisons between constants
    survived that, which is what the copy was written for, but nothing absolute did.

    It also merged runs the old way, holding an event open while *any* sensor of the same
    kind was on. `events.derive` stopped doing that — a lingering sensor made a car parking
    into a two-hour detection — and the copy did not follow.

    A tool used to choose the shipped constants has to be running the shipped code.
    """
    return events_derive(None, transitions, window=window, longest=longest, clock=_NoSun())


def run(
    events: list[Event],
    *,
    quantile: float,
    scope: str = DEFAULT_RELEVANCE_SCOPE,
    floor: float = RELEVANCE_SENSITIVITY_FLOORS[DEFAULT_RELEVANCE_SENSITIVITY],
) -> tuple[list[tuple[Event, object]], object]:
    """Build a model, calibrate it, and score everything against itself."""
    now = max((event.started_at for event in events), default=0.0)
    model = build(events, now=now, scope=scope)
    calibrate(model, events, share=quantile, floor=floor)

    marked: list[tuple[Event, object]] = []
    for event, previous in preceded(events, scope=model.scope):
        result = score(event, model, previous=previous)
        if result.unusual:
            marked.append((event, result))
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
            # The household's clock, not UTC: the point of the line is that somebody can
            # recognise the event in their own timeline.
            when = dt_util.as_local(dt_util.utc_from_timestamp(event.started_at)).strftime(
                "%Y-%m-%d %H:%M"
            )
            print(f"    {when}  {result.total:6.2f}  {result.reason}")


def main() -> int:
    """Read a journal and report what the current constants would do to it."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("journal", type=Path, help="path to reolink_stamina_journal.db")
    # Defaulted from the shipped constants rather than restated. These drifted once — 20s
    # against a shipped 3s, 600s against a shipped 300s — so the tool used to choose the
    # numbers was reporting on numbers nobody was running.
    parser.add_argument(
        "--merge", type=float, default=EVENT_MERGE_SECONDS, help="merge window, seconds"
    )
    parser.add_argument(
        "--longest", type=float, default=EVENT_MAX_SECONDS, help="longest event, seconds"
    )
    parser.add_argument("--quantile", type=float, default=SCORE_QUANTILE, help="threshold quantile")
    parser.add_argument(
        "--sensitivity",
        choices=list(RELEVANCE_SENSITIVITY_FLOORS),
        default=DEFAULT_RELEVANCE_SENSITIVITY,
        help="which floor to judge against, by the name the options page uses",
    )
    # The clock a household lives by, which a copied database does not carry. Home Assistant
    # defaults to UTC until something tells it otherwise, and every hour in the report would
    # then be an offset away from the hour the panel shows.
    parser.add_argument(
        "--tz",
        default=str(dt.datetime.now().astimezone().tzinfo),
        help="the recorder's timezone, e.g. Europe/Rome (default: this machine's)",
    )
    parser.add_argument(
        "--scope",
        choices=RELEVANCE_SCOPES,
        default=DEFAULT_RELEVANCE_SCOPE,
        help="which cameras may be compared with each other",
    )
    parser.add_argument(
        "--sweep",
        choices=("merge", "quantile", "scope"),
        help="try a range of values for one constant and print only the summary of each",
    )
    args = parser.parse_args()

    if not args.journal.exists():
        parser.error(f"no such journal: {args.journal}")

    try:
        dt_util.set_default_time_zone(ZoneInfo(args.tz))
    except (ZoneInfoNotFoundError, ValueError):
        parser.error(f"unknown timezone: {args.tz}")
    floor = RELEVANCE_SENSITIVITY_FLOORS[args.sensitivity]

    transitions = read(args.journal)
    if not transitions:
        print("The journal is empty. Nothing to replay yet.")
        return 0

    if args.sweep == "merge":
        print("\n  merge   events  per event   marked   %")
        for window in (5.0, 10.0, 20.0, 30.0, 60.0, 120.0):
            events = derive(transitions, window=window, longest=args.longest)
            marked, _ = run(events, quantile=args.quantile, scope=args.scope, floor=floor)
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
            marked, _ = run(events, quantile=quantile, scope=args.scope, floor=floor)
            share = 100 * len(marked) / max(len(events), 1)
            print(
                f"  {quantile:8.3f} {len(marked):8d} {share:6.2f} {len(marked) / span_weeks:11.1f}"
            )
        return 0

    if args.sweep == "scope":
        # The one sweep worth running on a Home Assistant that covers more than one property:
        # it says, in marks, what pooling those properties together is costing.
        events = derive(transitions, window=args.merge, longest=args.longest)
        print("\n  scope       groups   marked   %")
        for scope in RELEVANCE_SCOPES:
            marked, model = run(events, quantile=args.quantile, scope=scope, floor=floor)
            share = 100 * len(marked) / max(len(events), 1)
            print(f"  {scope:<10} {len(model.pooled):7d} {len(marked):8d} {share:6.2f}")
        return 0

    events = derive(transitions, window=args.merge, longest=args.longest)
    marked, model = run(events, quantile=args.quantile, scope=args.scope, floor=floor)
    report(events, len(transitions), marked, model, window=args.merge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
