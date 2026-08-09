"""How many clips cloud sync will handle at once.

The arithmetic, not the reading of the machine. What a container has left is unavoidably
platform-specific and says nothing interesting; turning a number of free bytes into a number
of slots is where a mistake would cost something — either a Pi cheerfully starting four
uploads it cannot hold, or a capable machine pinned to one for no reason.
"""

from __future__ import annotations

from custom_components.reolink_stamina.cloud.capacity import expected_clip_bytes, slots
from custom_components.reolink_stamina.const import (
    SYNC_MAX_CONCURRENT_CLIPS,
    SYNC_MEMORY_CEILING,
)

MB = 1024 * 1024
GB = 1024 * MB


def test_a_machine_we_cannot_measure_does_one_at_a_time() -> None:
    """The behaviour cloud sync had before any of this: never wrong, only slow."""
    assert slots(None, 4 * MB, 8) == 1


def test_a_roomy_machine_with_small_clips_reaches_the_ceiling() -> None:
    """A sub-stream install uploads four-megabyte clips; the memory budget is not the limit."""
    assert slots(8 * GB, 4 * MB, 8) == SYNC_MAX_CONCURRENT_CLIPS


def test_a_tight_machine_with_large_clips_still_gets_one() -> None:
    """Zero slots would mean nothing ever uploads, which is worse than uploading slowly."""
    assert slots(200 * MB, 180 * MB, 4) == 1


def test_the_budget_is_a_share_rather_than_everything_free() -> None:
    """Cloud sync is a guest on this machine, and Home Assistant is doing other things."""
    # A quarter of 400 MB is 100 MB, which holds two 40 MB clips and not three.
    assert slots(400 * MB, 40 * MB, 8) == 2


def test_a_very_large_machine_is_still_capped() -> None:
    """Past a handful of clips the recorder lock and the link are the limits anyway."""
    assert slots(512 * GB, 1 * MB, 64) == SYNC_MAX_CONCURRENT_CLIPS
    # And the ceiling is what bounds the budget, not the machine's own memory.
    assert SYNC_MEMORY_CEILING < 512 * GB


def test_the_cores_bound_it_because_a_cut_clip_is_an_ffmpeg() -> None:
    """A continuously recording camera's clip is cut by a subprocess of its own.

    Four of those on a two-core box is not concurrency, and one core has to be left for
    everything else Home Assistant is doing.
    """
    assert slots(8 * GB, 1 * MB, 2) == 1
    assert slots(8 * GB, 1 * MB, 3) == 2
    # A machine that will not say how many cores it has is not punished for it.
    assert slots(8 * GB, 1 * MB, None) == SYNC_MAX_CONCURRENT_CLIPS


def test_a_clip_size_of_nothing_cannot_divide_by_zero() -> None:
    """Defensive: an empty upload would otherwise mean unbounded concurrency."""
    assert slots(8 * GB, 0, 8) == 1


def test_the_expected_size_is_the_largest_recent_clip() -> None:
    """The budget has to hold whatever arrives next, and clips vary by an order of magnitude.

    An average would admit a job on the strength of a doorbell press and then be handed a car
    manoeuvring on the drive.
    """
    assert expected_clip_bytes([2 * MB, 3 * MB, 40 * MB], 32 * MB) == 40 * MB


def test_nothing_measured_yet_uses_the_pessimistic_assumption() -> None:
    """Concurrency is earned by uploading small clips, not granted and then discovered."""
    assert expected_clip_bytes([], 32 * MB) == 32 * MB
