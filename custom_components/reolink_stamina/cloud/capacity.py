"""How many clips this machine can have in flight at once.

Cloud sync used to handle one clip at a time, which is the right answer on the smallest
machine and a poor one everywhere else: a person crossing three cameras produces three clips
on one recorder, and the third waited out the first two entirely — fetch, upload and all —
before it was even searched for.

The constraint worth budgeting is **memory**. A clip is assembled in memory and held there
until the upload finishes, so every job in flight costs its own size; the recorder is already
protected from concurrency by the per-NVR lock, and overlapping uploads is exactly what makes
a slow link finish sooner rather than later. So the number of slots falls out of how much
memory the machine actually has free and how big this syncer's clips actually are, rather than
out of a constant somebody guessed.

Two halves, deliberately separated: reading what the machine has is unavoidably
platform-specific and cannot be tested meaningfully, while turning that number into a count of
slots is arithmetic and is where the mistakes would be. `slots()` therefore takes the numbers
rather than fetching them, and has tests.

Where the machine cannot be read at all, the answer is one — the behaviour cloud sync had
before any of this, which is never wrong, only slow.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..const import (
    SYNC_MAX_CONCURRENT_CLIPS,
    SYNC_MEMORY_CEILING,
    SYNC_MEMORY_SHARE,
)

_LOGGER = logging.getLogger(__name__)

# cgroup v2, then v1. Read before the host's own figures because a container is usually given
# far less than the machine it runs on, and Home Assistant is usually in one: `/proc/meminfo`
# inside a container cheerfully reports the whole host, which is how a 256 MB container decides
# it has 16 GB to play with.
_CGROUP2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_CGROUP1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

# A cgroup with no limit reports a number close to the whole address space rather than saying
# so. Anything above this is "unlimited", and the host's figures are the honest answer.
_UNLIMITED = 1 << 60


def _read_int(path: Path) -> int | None:
    """Return one integer from a /sys or /proc file, or None if it cannot be read."""
    try:
        text = path.read_text().strip()
    except (OSError, ValueError):
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _cgroup_available() -> int | None:
    """Return what a container has left of its own memory limit, if it is in one."""
    for limit_path, usage_path in (
        (_CGROUP2_MAX, _CGROUP2_CURRENT),
        (_CGROUP1_LIMIT, _CGROUP1_USAGE),
    ):
        limit = _read_int(limit_path)
        if limit is None or limit <= 0 or limit >= _UNLIMITED:
            continue
        used = _read_int(usage_path) or 0
        return max(0, limit - used)
    return None


def _host_available() -> int | None:
    """Return the host's free memory.

    `sysconf` rather than `/proc/meminfo` because it answers on macOS as well as Linux, and a
    development machine reporting nothing would otherwise be pinned to one slot for no reason.
    `SC_AVPHYS_PAGES` is free pages rather than total, which is the number worth budgeting a
    share of.
    """
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or size <= 0:
        return None
    return pages * size


def available_bytes() -> int | None:
    """Return the memory this process can reasonably expect to be able to use.

    None where nothing could be read, which the caller treats as "assume the smallest machine".
    """
    return _cgroup_available() or _host_available()


def slots(available: int | None, expected_clip: int, cpus: int | None = None) -> int:
    """Return how many clips may be in flight at once.

    `available` is free memory in bytes, or None where it could not be read. `expected_clip` is
    what one clip is expected to cost — measured from recent uploads rather than assumed, so a
    syncer uploading four-megabyte sub-stream clips earns concurrency that one uploading whole
    main-stream files does not.

    Never zero: something has to be able to run, and one at a time is the behaviour this
    replaced. Never more than one per CPU either, because a continuously recording camera's
    clip is cut by an ffmpeg of its own and four of those on a two-core box is not concurrency,
    it is contention.
    """
    if available is None or available <= 0 or expected_clip <= 0:
        return 1

    budget = min(int(available * SYNC_MEMORY_SHARE), SYNC_MEMORY_CEILING)
    fits = budget // expected_clip

    ceiling = SYNC_MAX_CONCURRENT_CLIPS
    if cpus:
        # One core has to be left for everything else Home Assistant is doing.
        ceiling = min(ceiling, max(1, cpus - 1))

    return max(1, min(int(fits), ceiling))


def expected_clip_bytes(sizes: list[int], assumed: int) -> int:
    """Return what the next clip should be budgeted at, given the recent ones.

    The largest rather than the average. The budget has to hold whatever arrives next, and
    clips vary by an order of magnitude on the same camera — a doorbell press against a car
    manoeuvring on the drive — so an average would admit jobs that then do not fit.
    """
    return max([*sizes, 1]) if sizes else assumed


def describe(available: int | None, expected_clip: int, cpus: int | None = None) -> str:
    """Return a line for the log and for diagnostics, so a slot count is never a mystery."""
    if available is None:
        return f"1 clip at a time (memory unreadable, {expected_clip / 1e6:.0f} MB per clip)"
    return (
        f"{slots(available, expected_clip, cpus)} clips at a time "
        f"({available / 1e6:.0f} MB free, {expected_clip / 1e6:.0f} MB per clip, "
        f"{cpus or '?'} CPUs)"
    )
