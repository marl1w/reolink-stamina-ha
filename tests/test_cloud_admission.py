"""Tests for what a recorder's syncer watches, and for when the switch no longer has a say.

The switch answers one question — "may I take on a new event now?" — and answers it once, as
the event opens. Asking it again when the clip is ready is the failure most of these exist for,
and it is a silent one: nothing is queued, no error is recorded, and the footage is simply not
there afterwards. Watching the wrong set of sensors fails the same silent way.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.reolink_stamina.cloud.engine import NvrSyncer, SyncJob, WatchedSensor
from custom_components.reolink_stamina.cloud.windows import ClipWindow

ENGINE = "custom_components.reolink_stamina.cloud.engine"

ENTRY = "nvr-entry"
CHANNEL = 7
KEY = f"{ENTRY}|{CHANNEL}"
NVR = "main-nvr"
PERSON = "binary_sensor.main_front_gate_person"
PET = "binary_sensor.second_front_gate_pet"
CAMERA = "Main - Front Gate"

# The arrival this was reported for, to the second.
BASE = dt.datetime(2026, 8, 4, 21, 51, 0)


def make_syncer(*, watching: bool = True, kinds: set[str] | None = None) -> NvrSyncer:
    """Return a syncer watching one camera, with nothing that reaches a network."""
    syncer = NvrSyncer(
        MagicMock(),
        MagicMock(),
        MagicMock(subentry_id="main"),
        nvr_name="Main NVR",
        entry_id=ENTRY,
        destination=MagicMock(label="OneDrive"),
        kinds=kinds or {"person"},
        quota=15 * 1024**3,
        folder="Reolink",
        stream="sub",
        lead=10.0,
        tail=10.0,
    )
    if watching:
        # Normally filled in by `_resolve_sensors` from the Reolink integration's entities.
        syncer._sensors = {
            PERSON: WatchedSensor(
                entry_id=ENTRY, channel=CHANNEL, nvr=NVR, camera=CAMERA, kind="person"
            )
        }
    return syncer


def fake_nvr(status: str = "ok") -> SimpleNamespace:
    """Return a recorder shaped the way the registry reports one."""
    return SimpleNamespace(
        entry_id=ENTRY,
        status=status,
        name=NVR,
        cameras=[SimpleNamespace(channel=CHANNEL, name=CAMERA)],
    )


def detection(entity_id: str, *, on: bool) -> MagicMock:
    """Return a state change event shaped the way the state tracker delivers it."""
    return MagicMock(
        data={
            "entity_id": entity_id,
            "old_state": MagicMock(state="off" if on else "on"),
            "new_state": MagicMock(state="on" if on else "off"),
        }
    )


def at(seconds: float) -> dt.datetime:
    """Return a moment relative to the first detection."""
    return BASE + dt.timedelta(seconds=seconds)


def test_the_arrival_survives_the_disarm_that_followed_it(freezer) -> None:
    """The reported failure, replayed from the logbook.

    A person at 21:51:01, the alarm disarmed at 21:51:07, the guard
    automation turned the switch off at 21:51:22 while a person was still in frame, and the
    window did not settle until 21:51:46. Consulting the switch at that point discarded the one
    clip that mattered — the footage of the arrival.
    """
    syncer = make_syncer()

    freezer.move_to(at(1))
    syncer._async_state_changed(detection(PERSON, on=True))
    freezer.move_to(at(5))
    syncer._async_state_changed(detection(PERSON, on=False))
    freezer.move_to(at(19))
    syncer._async_state_changed(detection(PERSON, on=True))

    # 21:51:22 — the guard automation reacts to the disarm. The event is already under way.
    syncer.accepting = False

    freezer.move_to(at(26))
    syncer._async_state_changed(detection(PERSON, on=False))
    syncer._async_tick(None)
    assert len(syncer._queue) == 0, "the tail has not elapsed yet"

    freezer.move_to(at(47))
    syncer._async_tick(None)

    assert len(syncer._queue) == 1, "an admitted event must become a clip whatever the switch says"
    job = syncer._queue[0]
    assert job.camera == CAMERA
    assert job.entry_id == ENTRY
    assert job.channel == CHANNEL
    assert job.window.start == at(-9), "ten seconds before the first detection"
    assert job.window.end == at(36), "ten seconds after the last one cleared"


def test_a_detection_while_the_switch_is_off_is_ignored(freezer) -> None:
    """What the switch is for: while it is off, nothing is taken on or even gathered."""
    syncer = make_syncer()
    syncer.accepting = False

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))
    assert syncer._windows.pending == 0, "no window should be gathering for a recorder not syncing"

    freezer.move_to(at(60))
    syncer._async_state_changed(detection(PERSON, on=False))
    syncer._async_tick(None)

    assert len(syncer._queue) == 0


def test_the_switch_coming_back_on_admits_the_next_event(freezer) -> None:
    """Refusing one event must not latch; the next is judged on its own."""
    syncer = make_syncer()
    syncer.accepting = False

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))
    freezer.move_to(at(10))
    syncer._async_state_changed(detection(PERSON, on=False))

    syncer.accepting = True
    freezer.move_to(at(60))
    syncer._async_state_changed(detection(PERSON, on=True))
    freezer.move_to(at(70))
    syncer._async_state_changed(detection(PERSON, on=False))

    freezer.move_to(at(91))
    syncer._async_tick(None)

    assert len(syncer._queue) == 1
    assert syncer._queue[0].window.start == at(50), "starts at the detection that was accepted"


def test_a_finished_window_is_not_judged_a_second_time() -> None:
    """Anything reaching the queue was admitted when it opened; the switch has no say left."""
    syncer = make_syncer()
    syncer.accepting = False

    syncer._enqueue(ClipWindow(key=KEY, start=BASE, end=at(30), kinds=("person",)))

    assert len(syncer._queue) == 1
    assert syncer._queue[0].camera == CAMERA


def test_the_kind_recorded_is_the_resolved_one_not_the_entity_id_suffix(freezer) -> None:
    """Reolink calls it `pet` on one recorder and `animal` on another; both mean animal.

    One recorder exposes `..._animal` and another `..._pet` for the same detection, so reading the
    kind off the end of the entity id made two cameras disagree about what had been seen — and
    the resolved kind was sitting there unused, having already been matched against the chosen
    detection types.
    """
    syncer = make_syncer(watching=False, kinds={"animal"})
    syncer._sensors = {
        PET: WatchedSensor(
            entry_id=ENTRY,
            channel=CHANNEL,
            nvr="second-nvr",
            camera="Second - Front Gate",
            kind="animal",
        )
    }

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PET, on=True))
    freezer.move_to(at(10))
    syncer._async_state_changed(detection(PET, on=False))
    freezer.move_to(at(31))
    syncer._async_tick(None)

    assert len(syncer._queue) == 1
    assert syncer._queue[0].window.kinds == ("animal",), "not `pet`, whatever the entity is called"


def test_a_recorder_that_was_down_at_startup_is_picked_up_later() -> None:
    """An unreachable recorder is skipped, and skipping it must not be permanent.

    Resolution legitimately comes up empty when the NVR is down — its detection sensors are not
    there to find. Doing it only once at setup left the syncer watching nothing at all, with no
    error to say why, until the integration was reloaded.
    """
    syncer = make_syncer(watching=False)
    discovered: list[list[SimpleNamespace]] = [[fake_nvr(status="error")]]
    unsubscribe = MagicMock()

    with (
        patch(f"{ENGINE}.async_discover_nvrs", side_effect=lambda _hass: discovered[0]),
        patch(f"{ENGINE}.async_detection_entities", return_value={PERSON: "person"}),
        patch(f"{ENGINE}.async_track_state_change_event", return_value=unsubscribe) as track,
    ):
        syncer._async_watch_sensors()
        assert syncer._sensors == {}, "a recorder that is not ok has nothing to watch"
        assert track.call_count == 0, "and nothing to subscribe to"

        # The recorder comes back. The next sweep finds its sensors and starts watching them.
        discovered[0] = [fake_nvr()]
        syncer._async_watch_sensors()

        assert set(syncer._sensors) == {PERSON}
        assert syncer._sensors[PERSON].kind == "person"
        assert syncer.nvr_name == NVR
        assert track.call_count == 1

        # An unchanged set must not churn the subscription on every sweep.
        syncer._async_watch_sensors()
        assert track.call_count == 1
        assert unsubscribe.call_count == 0

        # A camera that goes away is released rather than left subscribed.
        discovered[0] = []
        syncer._async_watch_sensors()
        assert syncer._sensors == {}
        assert unsubscribe.call_count == 1
        assert track.call_count == 1


async def test_an_event_over_midnight_looks_in_both_days() -> None:
    """Footage of an arrival at 23:59:55 is written into the next day.

    The search is per day, and only the day the window *started* in was asked — so an event
    that ran over midnight found nothing covering it, spent twelve attempts finding out, and
    reported "no recording covers this event" three times over.
    """
    syncer = make_syncer()
    late = dt.datetime(2026, 8, 4, 23, 59, 45)
    job = SyncJob(
        entry_id=ENTRY,
        channel=CHANNEL,
        nvr=NVR,
        camera=CAMERA,
        window=ClipWindow(
            key=KEY, start=late, end=late + dt.timedelta(seconds=40), kinds=("person",)
        ),
    )

    # The recording lives in the new day, which is where the recorder filed it.
    tomorrow = {
        "start": "2026-08-05T00:00:00",
        "end": "2026-08-05T00:05:00",
        "name": "after-midnight",
        "duration": 300.0,
        "start_id": "20260805000000",
    }
    asked: list[dt.date] = []

    async def fake_search(_hass, _entry, _channel, _stream, date, _split, **_kwargs):
        asked.append(date)
        return ([tomorrow] if date == dt.date(2026, 8, 5) else []), 0

    with patch("custom_components.reolink_stamina.vod.async_search_day", new=fake_search):
        found = await syncer._async_settled_recording(job)

    assert asked == [dt.date(2026, 8, 4), dt.date(2026, 8, 5)], "both sides of the boundary"
    assert found is not None
    assert found["name"] == "after-midnight"


async def test_an_ordinary_event_asks_for_one_day_only() -> None:
    """Searching costs the recorder real work, so the midnight case must not tax the rest."""
    syncer = make_syncer()
    job = SyncJob(
        entry_id=ENTRY,
        channel=CHANNEL,
        nvr=NVR,
        camera=CAMERA,
        window=ClipWindow(key=KEY, start=BASE, end=at(40), kinds=("person",)),
    )
    covering = {
        "start": "2026-08-04T21:49:59",
        "end": "2026-08-04T21:54:59",
        "name": "covering",
        "duration": 300.0,
        "start_id": "20260804214959",
    }
    asked: list[dt.date] = []

    async def fake_search(_hass, _entry, _channel, _stream, date, _split, **_kwargs):
        asked.append(date)
        return [covering], 0

    with patch("custom_components.reolink_stamina.vod.async_search_day", new=fake_search):
        found = await syncer._async_settled_recording(job)

    assert asked == [dt.date(2026, 8, 4)]
    assert found["name"] == "covering"


async def test_a_restart_mid_event_keeps_the_clip(freezer) -> None:
    """`async_stop` flushes open windows through the same door, so it must not be gated either.

    Home Assistant restarting while someone is in frame, with the switch off by then, is the
    same loss by a different route.
    """
    syncer = make_syncer()

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))
    syncer.accepting = False

    freezer.move_to(at(30))
    await syncer.async_stop()

    assert len(syncer._queue) == 1, "a restart should not discard what was already accepted"
    assert syncer._queue[0].window.truncated is True, "the person was still in frame"
