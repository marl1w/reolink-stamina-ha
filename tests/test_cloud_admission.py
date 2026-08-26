"""Tests for what a recorder's syncer watches, and for when the switch no longer has a say.

The switch answers one question — "may I take on a new event now?" — and answers it once, as
the event opens. Asking it again when the clip is ready is the failure most of these exist for,
and it is a silent one: nothing is queued, no error is recorded, and the footage is simply not
there afterwards. Watching the wrong set of sensors fails the same silent way.

The second half is the other admission rule: a kind nobody chose to sync, uploaded because the
model found that event unusual for its camera. That one cannot be decided when the window
opens — the detection has not been journalled yet, let alone scored — so it is decided in the
worker, and these pin down both that it is asked and that it is not asked needlessly.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.reolink_stamina.cloud.engine import NvrSyncer, SyncJob, WatchedSensor
from custom_components.reolink_stamina.cloud.windows import ClipWindow
from custom_components.reolink_stamina.const import DOMAIN

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


def make_syncer(
    *,
    watching: bool = True,
    kinds: set[str] | None = None,
    unusual: bool = False,
    unusual_kinds: set[str] | None = None,
) -> NvrSyncer:
    """Return a syncer watching one camera, with nothing that reaches a network."""
    syncer = NvrSyncer(
        MagicMock(),
        MagicMock(),
        MagicMock(subentry_id="main"),
        nvr_name="Main NVR",
        entry_id=ENTRY,
        destination=AsyncMock(label="OneDrive"),
        kinds=kinds or {"person"},
        quota=15 * 1024**3,
        folder="Reolink",
        stream="sub",
        lead=10.0,
        tail=10.0,
        unusual=unusual,
        unusual_kinds=unusual_kinds,
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


def job_for(*kinds: str) -> SyncJob:
    """Return a finished clip waiting to be judged, holding the kinds given."""
    return SyncJob(
        entry_id=ENTRY,
        channel=CHANNEL,
        nvr=NVR,
        camera=CAMERA,
        window=ClipWindow(key=KEY, start=BASE, end=at(30), kinds=tuple(sorted(kinds))),
    )


class FakeModel:
    """Stands in for the relevance runtime, and records what it was asked."""

    def __init__(self, *, state: str = "active", marked: dict[str, bool] | None = None) -> None:
        """Answer `state` for every camera, and mark the kinds given as unusual."""
        self._state = state
        self._marked = marked or {}
        self.windows = 0
        self.flushes = 0

    # --- the shape `_async_wanted` reaches for

    @property
    def analysis(self):
        """The model, as the runtime exposes it."""
        return self

    @property
    def journal(self):
        """The journal, likewise."""
        return self

    def state(self, _camera: str) -> str:
        """Return what the panel would say about this camera."""
        return self._state

    async def async_flush(self) -> None:
        """Write out the buffered transitions, as the real journal does on demand."""
        self.flushes += 1

    async def async_window(self, **_kwargs):
        """Score the events in a window, oldest first."""
        self.windows += 1
        return [
            (SimpleNamespace(kind=kind), SimpleNamespace(unusual=unusual))
            for kind, unusual in self._marked.items()
        ]


def with_model(syncer: NvrSyncer, model: FakeModel | None) -> FakeModel | None:
    """Give a syncer a Home Assistant whose relevance runtime is `model`."""
    syncer.hass = SimpleNamespace(data={DOMAIN: SimpleNamespace(relevance=model)})
    return model


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
        patch(f"{ENGINE}.async_discover_devices", side_effect=lambda _hass: discovered[0]),
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


async def test_a_recording_that_already_covers_the_window_is_taken_at_once() -> None:
    """The common case, and it must not pay for the case that is not.

    A recorder writing continuously always has a file running past the end of the window, so
    the first look is the answer and there is nothing to wait for.
    """
    syncer = make_syncer()
    job = job_for("person")
    covering = {
        "start": "2026-08-04T21:49:59",
        "end": "2026-08-04T21:54:59",
        "name": "covering",
        "duration": 300.0,
        "start_id": "20260804214959",
    }
    slept: list[float] = []

    async def fake_search(_hass, _entry, _channel, _stream, _date, _split, **_kwargs):
        return [covering], 0

    async def fake_sleep(seconds):
        slept.append(seconds)

    with (
        patch("custom_components.reolink_stamina.vod.async_search_day", new=fake_search),
        patch(f"{ENGINE}.asyncio.sleep", new=fake_sleep),
    ):
        found = await syncer._async_settled_recording(job)

    assert found["name"] == "covering"
    assert slept == [], "nothing to wait for"


async def test_a_recording_that_ended_early_is_not_called_finished_by_two_quick_looks() -> None:
    """A recording that stopped growing needs two looks far enough apart to prove it.

    The pacing starts short so the ordinary case is fast, which leaves the fallback needing a
    spacing rule of its own: a file being written to can easily look identical twice in two
    seconds, and taking it then uploads a clip that stops in the middle of the event.
    """
    syncer = make_syncer()
    # Ends before the window does, so the exact test never fires and only stability can end
    # the search.
    short = {
        "start": "2026-08-04T21:50:55",
        "end": "2026-08-04T21:51:20",
        "name": "ended-early",
        "duration": 25.0,
        "start_id": "20260804215055",
    }
    searches: list[int] = []
    slept: list[float] = []

    async def fake_search(_hass, _entry, _channel, _stream, _date, _split, **_kwargs):
        searches.append(1)
        return [short], 0

    async def fake_sleep(seconds):
        slept.append(seconds)

    with (
        patch("custom_components.reolink_stamina.vod.async_search_day", new=fake_search),
        patch(f"{ENGINE}.asyncio.sleep", new=fake_sleep),
    ):
        found = await syncer._async_settled_recording(job_for("person"))

    assert found["name"] == "ended-early"
    assert slept == [2.0, 3.0, 5.0], "short looks first, and ten seconds of spacing before trusting"
    assert len(searches) == 4


# --------------------------------------------------------- uploading the unusual


async def test_a_chosen_kind_is_uploaded_without_asking_the_model() -> None:
    """The rule that was always here, unchanged and — importantly — not made slower.

    A person clip on a syncer that syncs people must not wait on a journal read to be told
    what it already knows.
    """
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    model = with_model(syncer, FakeModel(marked={"person": False}))

    job = job_for("person")

    assert await syncer._async_wanted(job) is True
    assert job.unusual is False, "uploaded because it was chosen, not because it was odd"
    assert model.windows == 0, "the model must not be consulted for a kind already synced"


async def test_a_kind_only_in_the_unusual_list_is_uploaded_when_it_is_marked() -> None:
    """The point of the feature: motion is not worth syncing, except when it is."""
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    model = with_model(syncer, FakeModel(marked={"motion": True}))

    job = job_for("motion")

    assert await syncer._async_wanted(job) is True
    assert job.unusual is True, "which is what puts the _u in the file name"
    assert model.flushes == 1, "the journal buffers for half a minute; this event is seconds old"


async def test_an_ordinary_event_of_an_unchosen_kind_is_dropped() -> None:
    """Nothing is fetched, searched for or uploaded — the whole point of deciding here."""
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    with_model(syncer, FakeModel(marked={"motion": False}))

    job = job_for("motion")

    assert await syncer._async_wanted(job) is False
    assert job.unusual is False


async def test_only_the_kinds_chosen_for_the_rule_count() -> None:
    """A marked event of a kind nobody asked about is still not uploaded.

    Otherwise the multiselect would be decoration: switching the rule on would quietly mean
    "upload every unusual thing", which is not what it says.
    """
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"animal"})
    with_model(syncer, FakeModel(marked={"motion": True}))

    assert await syncer._async_wanted(job_for("motion")) is False


async def test_a_camera_still_collecting_is_answered_without_a_journal_read() -> None:
    """Nothing can be called unusual for a fortnight, and asking anyway costs a query.

    On a 24/7 camera with motion in the list that would be a database read for every
    detection it makes, all of them answering no.
    """
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    model = with_model(syncer, FakeModel(state="collecting", marked={"motion": True}))

    assert await syncer._async_wanted(job_for("motion")) is False
    assert model.windows == 0, "answered from the model in memory"
    assert model.flushes == 0


async def test_with_the_rule_off_admission_is_exactly_what_it_was() -> None:
    """An installation that did not ask for this must behave identically to before."""
    syncer = make_syncer(kinds={"person"}, unusual=False, unusual_kinds={"motion"})
    model = with_model(syncer, FakeModel(marked={"motion": True}))

    assert await syncer._async_wanted(job_for("motion")) is False
    assert model.windows == 0


async def test_a_recorder_with_no_model_running_uploads_only_what_it_was_told_to() -> None:
    """Relevance failing to start must not turn into clips going missing or exceptions."""
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    with_model(syncer, None)

    assert await syncer._async_wanted(job_for("motion")) is False
    assert await syncer._async_wanted(job_for("person")) is True


def test_the_unusual_kinds_are_watched_too() -> None:
    """Otherwise the rule could never fire.

    A kind that is not synced has no sensor subscribed to it, so no window ever opens for it
    and there is nothing to score — the feature would be switched on and silently do nothing.
    """
    syncer = make_syncer(watching=False, kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    sensors = {PERSON: "person", "binary_sensor.main_front_gate_motion": "motion"}

    with (
        patch(f"{ENGINE}.async_discover_devices", return_value=[fake_nvr()]),
        patch(f"{ENGINE}.async_detection_entities", return_value=sensors),
        patch(f"{ENGINE}.async_track_state_change_event", return_value=MagicMock()),
    ):
        syncer._async_watch_sensors()

    assert set(syncer._sensors) == set(sensors), "both rules' kinds are subscribed to"


def test_with_the_rule_off_only_the_chosen_kinds_are_watched() -> None:
    """A recorder not using this must not pay for a subscription to every motion detection."""
    syncer = make_syncer(watching=False, kinds={"person"}, unusual=False, unusual_kinds={"motion"})
    sensors = {PERSON: "person", "binary_sensor.main_front_gate_motion": "motion"}

    with (
        patch(f"{ENGINE}.async_discover_devices", return_value=[fake_nvr()]),
        patch(f"{ENGINE}.async_detection_entities", return_value=sensors),
        patch(f"{ENGINE}.async_track_state_change_event", return_value=MagicMock()),
    ):
        syncer._async_watch_sensors()

    assert set(syncer._sensors) == {PERSON}


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


# ------------------------------------- the switch, once unusual uploads are asked for


def test_the_switch_still_gathers_nothing_for_a_recorder_that_did_not_ask(freezer) -> None:
    """Unchanged for everyone who never switched the second rule on.

    Off means nothing is gathered at all, which is what makes a recorder that is not syncing
    free to watch.
    """
    syncer = make_syncer(unusual=False)
    syncer.accepting = False

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))

    assert syncer._windows.pending == 0


def test_a_recorder_uploading_the_unusual_keeps_gathering_while_the_switch_is_off(
    freezer,
) -> None:
    """An event that is never gathered can never be scored.

    So the standing instruction to keep the odd one out has to survive the switch, or it would
    quietly stop applying at exactly the times somebody arms an alarm around.
    """
    syncer = make_syncer(unusual=True, unusual_kinds={"person"})
    syncer.accepting = False

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))
    assert syncer._windows.pending == 1, "gathered despite the switch"

    freezer.move_to(at(10))
    syncer._async_state_changed(detection(PERSON, on=False))
    freezer.move_to(at(30))
    syncer._async_tick(None)

    assert len(syncer._queue) == 1
    assert syncer._queue[0].window.admitted is False, "the switch was off when this opened"


async def test_a_disarmed_window_is_uploaded_only_if_it_turns_out_to_be_unusual() -> None:
    """The whole point of gathering it: ordinary footage from a disarmed house is discarded.

    Nothing is fetched for it either — the decision happens before the recorder is asked
    anything at all.
    """
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"person"})
    with_model(syncer, FakeModel(marked={"person": False}))

    job = job_for("person")
    job.window = ClipWindow(key=KEY, start=BASE, end=at(30), kinds=("person",), admitted=False)

    assert await syncer._async_wanted(job) is False


async def test_a_disarmed_window_that_is_unusual_goes_up() -> None:
    """A disarmed house still gets an off-site copy of the thing that was not ordinary."""
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"person"})
    with_model(syncer, FakeModel(marked={"person": True}))

    job = job_for("person")
    job.window = ClipWindow(key=KEY, start=BASE, end=at(30), kinds=("person",), admitted=False)

    assert await syncer._async_wanted(job) is True
    assert job.unusual is True, "which is what marks the file _u"


async def test_re_arming_mid_event_does_not_turn_a_disarmed_window_into_a_routine_upload(
    freezer,
) -> None:
    """Admission is decided once, in both directions.

    A person arrives while the alarm is disarmed and the switch off; the household arms it
    while they are still in frame. Reading the switch when the clip settles would upload the
    whole disarmed event as though it had been accepted all along.
    """
    syncer = make_syncer(kinds={"person"}, unusual=True, unusual_kinds={"motion"})
    syncer.accepting = False

    freezer.move_to(BASE)
    syncer._async_state_changed(detection(PERSON, on=True))
    # The alarm is armed again while the person is still in frame.
    syncer.accepting = True

    freezer.move_to(at(10))
    syncer._async_state_changed(detection(PERSON, on=False))
    freezer.move_to(at(30))
    syncer._async_tick(None)

    job = syncer._queue[0]
    assert job.window.admitted is False

    # Person is a synced kind, but this window was never admitted — and person is not in the
    # unusual list either, so there is nothing to fall back on.
    with_model(syncer, FakeModel(marked={"person": True}))
    assert await syncer._async_wanted(job) is False
