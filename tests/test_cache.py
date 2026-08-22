"""Tests for the stale-while-revalidate cache.

This is the machinery behind the panel's core promise: a slow or offline NVR degrades
the freshness of the data, never the responsiveness of the UI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.reolink_stamina.cache import VodCache, calendar_key, day_key
from custom_components.reolink_stamina.const import (
    FILE_SCHEMA_VERSION,
    MAX_CONCURRENT_SEARCHES,
    STORAGE_KEY,
    STORAGE_VERSION,
    TTL_TODAY,
)

TODAY = dt_util.now().date()
PAST = TODAY - dt.timedelta(days=3)

FILES = [{"start": "2026-08-03T14:00:00+02:00", "name": "a.mp4", "size": 10}]


@pytest.fixture
def cache(hass: HomeAssistant) -> VodCache:
    """Return a fresh cache with nothing persisted."""
    return VodCache(hass)


# ------------------------------------------------------------------- freshness


async def test_nothing_cached_is_not_fresh(cache: VodCache) -> None:
    """The panel must be able to tell 'empty day' from 'not looked yet'."""
    assert cache.peek_day("entry", 0, "sub", TODAY) is None
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is False


async def test_todays_data_expires_quickly(cache: VodCache, hass) -> None:
    """Today is still being recorded into, so it must be re-checked often."""
    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is True

    # Age it past today's TTL.
    cache.peek_day("entry", 0, "sub", TODAY).fetched_at = time.time() - (TTL_TODAY + 1)
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is False


async def test_past_days_are_cached_aggressively(cache: VodCache, hass) -> None:
    """A day in the past cannot gain recordings, so it need not be searched again."""
    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()

    cache.peek_day("entry", 0, "sub", PAST).fetched_at = time.time() - 3600
    assert cache.is_day_fresh("entry", 0, "sub", PAST) is True


async def test_fresh_data_is_not_refetched(cache: VodCache, hass) -> None:
    """The point of the cache: no redundant round trips to the recorder."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)
    ) as search:
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()
        assert cache.async_ensure_day("entry", 0, "sub", PAST, 5) is None
        await hass.async_block_till_done()

    assert search.call_count == 1


async def test_force_refetches_even_when_fresh(cache: VodCache, hass) -> None:
    """The refresh button must actually reach the NVR."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)
    ) as search:
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5, force=True)
        await hass.async_block_till_done()

    assert search.call_count == 2


# ------------------------------------------------------------------ coalescing


async def test_concurrent_requests_share_one_search(cache: VodCache, hass) -> None:
    """Several subscribers wanting the same camera-day must not multiply the load."""
    gate = asyncio.Event()

    async def slow_search(*args, **kwargs):
        await gate.wait()
        return FILES, 0

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day", side_effect=slow_search
    ) as search:
        first = cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        second = cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        assert first is second
        gate.set()
        await hass.async_block_till_done()

    assert search.call_count == 1


async def test_searches_are_capped_per_nvr(cache: VodCache, hass) -> None:
    """A wide selection must not overwhelm the recorder itself."""
    active = 0
    peak = 0
    release = asyncio.Event()

    async def counting_search(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return FILES, 0

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=counting_search,
    ):
        for offset in range(MAX_CONCURRENT_SEARCHES + 4):
            cache.async_ensure_day("entry", 0, "sub", TODAY - dt.timedelta(days=offset), 5)
        await asyncio.sleep(0)
        release.set()
        await hass.async_block_till_done()

    assert peak <= MAX_CONCURRENT_SEARCHES


# ---------------------------------------------------------------- failure mode


async def test_a_failed_search_keeps_the_stale_data(cache: VodCache, hass) -> None:
    """Showing yesterday's answer beats showing nothing at all."""
    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=OSError("NVR unreachable"),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5, force=True)
        await hass.async_block_till_done()

    record = cache.peek_day("entry", 0, "sub", TODAY)
    assert record.files == FILES  # data survived
    assert "NVR unreachable" in record.error  # and the failure is reported


async def test_a_failure_with_no_cached_data_is_recorded(cache: VodCache, hass) -> None:
    """The panel needs to distinguish 'nothing recorded' from 'could not ask'."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=OSError("boom"),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    record = cache.peek_day("entry", 0, "sub", TODAY)
    assert record.files == []
    assert record.error == "boom"


async def test_a_search_failure_never_propagates(cache: VodCache, hass) -> None:
    """A background refresh must not surface as an unhandled task exception."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=RuntimeError("kaboom"),
    ):
        task = cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert task.exception() is None


# ------------------------------------------------------------------- listeners


async def test_listeners_are_told_which_key_changed(cache: VodCache, hass) -> None:
    """This is what drives the websocket patch for one camera-day."""
    seen: list[str] = []
    cache.async_add_listener(seen.append)

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert seen == [day_key("entry", 0, "sub", TODAY)]


async def test_a_finished_search_no_longer_looks_in_flight(cache: VodCache, hass) -> None:
    """The patch a listener builds is the last word on that camera-day.

    Listeners answer `async_is_fetching` while they are being called, so if the fetch were
    still registered at that point the panel's final answer would claim a refresh was
    running, with nothing left to correct it — the "Updating…" pill would never clear.
    """
    key = day_key("entry", 0, "sub", TODAY)
    fetching: list[bool] = []
    cache.async_add_listener(lambda changed: fetching.append(cache.async_is_fetching(changed)))

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert fetching == [False]
    assert cache.async_is_fetching(key) is False


async def test_a_failed_search_no_longer_looks_in_flight(cache: VodCache, hass) -> None:
    """A failure ends the refresh too, so the pill must clear on that path as well."""
    fetching: list[bool] = []
    cache.async_add_listener(lambda changed: fetching.append(cache.async_is_fetching(changed)))

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=OSError("NVR unreachable"),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert fetching == [False]


async def test_removing_a_listener_stops_notifications(cache: VodCache, hass) -> None:
    """Closing the panel must not leak subscriptions."""
    seen: list[str] = []
    remove = cache.async_add_listener(seen.append)
    remove()

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert seen == []


async def test_one_bad_listener_does_not_break_the_others(cache: VodCache, hass) -> None:
    """A failing subscriber must not stop the rest of the UI updating."""
    seen: list[str] = []

    def explode(key: str) -> None:
        raise ValueError("bad subscriber")

    cache.async_add_listener(explode)
    cache.async_add_listener(seen.append)

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(FILES, 0)):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert len(seen) == 1


# ----------------------------------------------------------------- persistence


async def test_persisted_results_are_restored(hass: HomeAssistant, hass_storage) -> None:
    """A restart must still paint instantly from disk."""
    key = day_key("entry", 0, "sub", PAST)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {key: {"files": FILES, "fetched_at": time.time(), "error": None}},
            "calendars": {},
        },
    }

    cache = VodCache(hass)
    await cache.async_load()
    assert cache.peek_day("entry", 0, "sub", PAST).files == FILES


async def test_ancient_results_are_pruned_on_load(hass: HomeAssistant, hass_storage) -> None:
    """The NVR cannot search that far back, so the cache should not grow forever."""
    ancient = TODAY - dt.timedelta(days=120)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {
                day_key("entry", 0, "sub", ancient): {
                    "files": FILES,
                    "fetched_at": 0,
                    "error": None,
                }
            },
            "calendars": {},
        },
    }

    cache = VodCache(hass)
    await cache.async_load()
    assert cache.peek_day("entry", 0, "sub", ancient) is None


async def test_ancient_calendars_are_pruned_too(hass: HomeAssistant, hass_storage) -> None:
    """Only days were pruned, so calendars grew for the life of the installation.

    A month whose last day has fallen out of the search window can never be asked about
    again, so its entry is pure growth in the file on disk.
    """
    ancient = TODAY - dt.timedelta(days=400)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {},
            "calendars": {
                calendar_key("entry", 0, ancient.year, ancient.month): {
                    "days": [1, 2, 3],
                    "fetched_at": 0,
                    "error": None,
                },
                "entry|0|not-a-month": {"days": [], "fetched_at": 0, "error": None},
            },
        },
    }

    cache = VodCache(hass)
    await cache.async_load()

    assert cache.peek_calendar("entry", 0, ancient.year, ancient.month) is None
    assert cache._calendars == {}, "and an unparseable key goes with it"


async def test_the_current_month_is_kept(hass: HomeAssistant, hass_storage) -> None:
    """Pruning must not reach the month being browsed.

    The cutoff is the month's *last* day, not its first: for most of any month the first is
    already outside a thirty-day window while the month itself is exactly what is on screen.
    """
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {},
            "calendars": {
                calendar_key("entry", 0, TODAY.year, TODAY.month): {
                    "days": [1, 2],
                    "fetched_at": time.time(),
                    "error": None,
                }
            },
        },
    }

    cache = VodCache(hass)
    await cache.async_load()

    record = cache.peek_calendar("entry", 0, TODAY.year, TODAY.month)
    assert record is not None
    assert record.days == [1, 2]


async def test_malformed_persisted_data_is_discarded(hass: HomeAssistant, hass_storage) -> None:
    """A corrupt cache file must not stop the panel loading."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {"days": {"not-a-key": "not-a-record"}, "calendars": {}},
    }

    cache = VodCache(hass)
    await cache.async_load()  # must not raise
    assert cache.peek_day("entry", 0, "sub", TODAY) is None


async def test_shutdown_cancels_in_flight_searches(cache: VodCache, hass) -> None:
    """Unloading the integration must not leave searches running."""
    gate = asyncio.Event()

    async def slow_search(*args, **kwargs):
        await gate.wait()
        return FILES, 0

    with patch("custom_components.reolink_stamina.cache.async_search_day", side_effect=slow_search):
        task = cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await asyncio.sleep(0)
        cache.async_shutdown()
        gate.set()
        # Waited on rather than given a fixed number of loop turns to unwind in. A paired
        # camera searches two devices through a gather, so how many turns cancellation takes
        # to travel back out is an implementation detail; that it arrives is not.
        _done, pending = await asyncio.wait([task], timeout=5)

    assert not pending, "the search never unwound"
    assert task.cancelled()


async def test_including_unlabelled_uses_a_separate_cache_entry(cache: VodCache, hass) -> None:
    """Discarded-unlabelled and include-unlabelled must not share a cache entry.

    A record stored with continuous footage discarded is not a valid answer to a request
    that wants it.
    """
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        return_value=(FILES, 7),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert cache.peek_day("entry", 0, "sub", TODAY) is not None
    assert cache.peek_day("entry", 0, "sub", TODAY).unlabelled == 7
    # The "give me everything" variant has not been fetched at all.
    assert cache.peek_day("entry", 0, "sub", TODAY, True) is None
    assert cache.is_day_fresh("entry", 0, "sub", TODAY, True) is False


async def test_freshness_survives_a_restart(hass: HomeAssistant, hass_storage) -> None:
    """Persisted timestamps must be comparable in a new process.

    Regression guard: these were written with time.monotonic(), which counts from boot.
    After a host reboot a stored value produced a negative age, so every cached day
    looked fresh forever and stale data was never refreshed.
    """
    key = day_key("entry", 0, "sub", TODAY)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {
                key: {
                    # Written 10 minutes ago by a previous run.
                    "files": FILES,
                    "fetched_at": time.time() - 600,
                    "error": None,
                }
            },
            "calendars": {},
        },
    }

    cache = VodCache(hass)
    await cache.async_load()

    # Data is served, but today's is correctly considered stale and gets refreshed.
    assert cache.peek_day("entry", 0, "sub", TODAY).files == FILES
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is False


async def test_a_future_timestamp_is_not_fresh_forever(hass: HomeAssistant, hass_storage) -> None:
    """A clock that moved backwards must not pin data as fresh indefinitely."""
    key = day_key("entry", 0, "sub", TODAY)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {key: {"files": FILES, "fetched_at": time.time() + 86400, "error": None}},
            "calendars": {},
        },
    }

    cache = VodCache(hass)
    await cache.async_load()
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is False


async def test_records_from_an_older_schema_are_refetched(
    hass: HomeAssistant, hass_storage
) -> None:
    """A cached record written before a field existed must not be served as current.

    This is the bug that made every clip unplayable: playback needs playback_id, and
    records cached before it existed had none, so the recorder refused them.
    """
    key = day_key("entry", 0, "sub", TODAY)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "days": {
                key: {
                    "files": [{"start": "x", "end": "y", "name": "n"}],
                    "fetched_at": time.time(),  # recent...
                    "error": None,
                    # ...but written by an older version.
                }
            },
            "calendars": {},
        },
    }

    cache = VodCache(hass)
    await cache.async_load()

    # Still served, because stale data beats no data...
    assert cache.peek_day("entry", 0, "sub", TODAY).files
    # ...but it is not considered fresh, so it gets refetched.
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is False


async def test_current_schema_is_fresh(cache: VodCache, hass) -> None:
    """A record written by this version is fresh until its TTL expires."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        return_value=(FILES, 0),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    record = cache.peek_day("entry", 0, "sub", TODAY)
    assert record.schema == FILE_SCHEMA_VERSION
    assert cache.is_day_fresh("entry", 0, "sub", TODAY) is True


async def test_an_empty_record_is_not_refetched_for_schema_alone(cache: VodCache, hass) -> None:
    """A day with genuinely no recordings should not be re-searched forever."""
    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=([], 0)):
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()

    assert cache.is_day_fresh("entry", 0, "sub", PAST) is True


# ------------------------------------------------- a camera whose recordings are on a recorder


PAIRED = ("nvr", 2)


def _rows(name: str, source: tuple[str, int]) -> list[dict]:
    """One serialised recording, tagged with the device that answered for it."""
    return [
        {
            "start": "2026-08-03T14:00:00+02:00",
            "name": name,
            "size": 10,
            "source_entry_id": source[0],
            "source_channel": source[1],
        }
    ]


def _searcher(answers: dict[tuple[str, int], object]):
    """Stand in for async_search_day, answering per device asked.

    An answer is either `(files, unlabelled)` or an exception to raise, so a test can put
    one device offline without putting the camera offline.
    """

    async def search(hass, entry_id, channel, *args, **kwargs):
        target = (kwargs["source_entry_id"], kwargs["source_channel"])
        answer = answers[target]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return search


def _paired(target=PAIRED):
    """Patch discovery so the camera under test is paired to `target`."""
    return patch(
        "custom_components.reolink_stamina.cache.async_paired_channel", return_value=target
    )


async def test_an_unpaired_camera_is_asked_once(cache: VodCache, hass) -> None:
    """The ordinary case must not pay for the paired one."""
    calls = []

    async def search(hass_, entry_id, channel, *args, **kwargs):
        calls.append((kwargs["source_entry_id"], kwargs["source_channel"]))
        return FILES, 0

    with (
        patch("custom_components.reolink_stamina.cache.async_paired_channel", return_value=None),
        patch("custom_components.reolink_stamina.cache.async_search_day", side_effect=search),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert calls == [("entry", 0)]


async def test_a_paired_camera_asks_both_and_keeps_the_one_that_answers(
    cache: VodCache, hass
) -> None:
    """Issue #4: the camera's own card is empty because it records to the recorder."""
    answers = {("entry", 0): ([], 0), PAIRED: (_rows("nvr.mp4", PAIRED), 0)}

    with (
        _paired(),
        patch(
            "custom_components.reolink_stamina.cache.async_search_day",
            side_effect=_searcher(answers),
        ),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    record = cache.peek_day("entry", 0, "sub", TODAY)
    assert [file["name"] for file in record.files] == ["nvr.mp4"]
    assert record.error is None
    # Filed under the camera the panel is showing, while the row remembers who answered.
    assert record.files[0]["source_entry_id"] == "nvr"
    assert record.files[0]["source_channel"] == 2


async def test_the_answering_device_is_remembered_for_the_next_day(cache: VodCache, hass) -> None:
    """Learned once per camera, so every later day costs one search rather than two."""
    answers = {("entry", 0): ([], 0), PAIRED: (_rows("nvr.mp4", PAIRED), 0)}
    asked: list[tuple[str, int]] = []

    async def search(hass_, entry_id, channel, *args, **kwargs):
        target = (kwargs["source_entry_id"], kwargs["source_channel"])
        asked.append(target)
        return answers[target]

    with (
        _paired(),
        patch("custom_components.reolink_stamina.cache.async_search_day", side_effect=search),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()
        assert sorted(asked) == [("entry", 0), PAIRED]

        asked.clear()
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()

    assert asked == [PAIRED]


async def test_a_day_with_nothing_on_it_settles_nothing(cache: VodCache, hass) -> None:
    """A quiet day is not evidence about where a camera records.

    Drawing a winner from it would pin the camera to whichever device was asked first and
    happened to have nothing, on the strength of no recordings at all.
    """
    answers = {("entry", 0): ([], 0), PAIRED: ([], 0)}
    asked: list[tuple[str, int]] = []

    async def search(hass_, entry_id, channel, *args, **kwargs):
        target = (kwargs["source_entry_id"], kwargs["source_channel"])
        asked.append(target)
        return answers[target]

    with (
        _paired(),
        patch("custom_components.reolink_stamina.cache.async_search_day", side_effect=search),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()
        asked.clear()
        await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
        await hass.async_block_till_done()

    assert sorted(asked) == [("entry", 0), PAIRED]
    assert cache.peek_day("entry", 0, "sub", TODAY).error is None


async def test_one_device_being_offline_does_not_fail_the_camera(cache: VodCache, hass) -> None:
    """The whole point of asking the recorder is that the camera may be unreachable."""
    from custom_components.reolink_stamina.reolink_registry import DeviceUnavailableError

    answers = {
        ("entry", 0): DeviceUnavailableError("camera is asleep"),
        PAIRED: (_rows("nvr.mp4", PAIRED), 0),
    }

    with (
        _paired(),
        patch(
            "custom_components.reolink_stamina.cache.async_search_day",
            side_effect=_searcher(answers),
        ),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    record = cache.peek_day("entry", 0, "sub", TODAY)
    assert record.error is None
    assert [file["name"] for file in record.files] == ["nvr.mp4"]


async def test_every_device_failing_is_still_an_error(cache: VodCache, hass) -> None:
    """Asking twice must not turn two failures into a silent empty day."""
    from custom_components.reolink_stamina.reolink_registry import DeviceUnavailableError

    answers = {
        ("entry", 0): DeviceUnavailableError("camera is asleep"),
        PAIRED: DeviceUnavailableError("recorder is offline"),
    }

    with (
        _paired(),
        patch(
            "custom_components.reolink_stamina.cache.async_search_day",
            side_effect=_searcher(answers),
        ),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    # The camera's own failure is reported: that is the device the user is looking at.
    assert cache.peek_day("entry", 0, "sub", TODAY).error == "camera is asleep"


async def test_the_recorder_wins_when_both_answer(cache: VodCache, hass) -> None:
    """A camera can write to its own card and to the recorder at once.

    Continuous recording lives on the recorder, so it holds the longer history and is the
    side someone pairing a camera is reaching for.
    """
    answers = {
        ("entry", 0): (_rows("card.mp4", ("entry", 0)), 0),
        PAIRED: (_rows("nvr.mp4", PAIRED), 0),
    }

    with (
        _paired(),
        patch(
            "custom_components.reolink_stamina.cache.async_search_day",
            side_effect=_searcher(answers),
        ),
    ):
        await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
        await hass.async_block_till_done()

    assert [f["name"] for f in cache.peek_day("entry", 0, "sub", TODAY).files] == ["nvr.mp4"]


async def test_re_enabling_the_channel_withdraws_the_redirect(cache: VodCache, hass) -> None:
    """Taking the recorder's copy back into use ends the pairing, and the redirect with it.

    The remembered device would otherwise go on being asked for a camera it no longer
    stands in for.
    """
    answers = {
        ("entry", 0): (_rows("card.mp4", ("entry", 0)), 0),
        PAIRED: (_rows("nvr.mp4", PAIRED), 0),
    }
    asked: list[tuple[str, int]] = []

    async def search(hass_, entry_id, channel, *args, **kwargs):
        target = (kwargs["source_entry_id"], kwargs["source_channel"])
        asked.append(target)
        return answers[target]

    with patch("custom_components.reolink_stamina.cache.async_search_day", side_effect=search):
        with _paired():
            await cache.async_ensure_day("entry", 0, "sub", TODAY, 5)
            await hass.async_block_till_done()
        assert cache._sources.get(("entry", 0)) == PAIRED

        asked.clear()
        # The user re-enables the recorder's copy: there is no pairing any more.
        with patch(
            "custom_components.reolink_stamina.cache.async_paired_channel", return_value=None
        ):
            await cache.async_ensure_day("entry", 0, "sub", PAST, 5)
            await hass.async_block_till_done()

    assert asked == [("entry", 0)]
    assert ("entry", 0) not in cache._sources
