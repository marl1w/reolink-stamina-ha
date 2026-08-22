"""Keeping a model built from the journal, and what the panel is told about each camera."""

from __future__ import annotations

import datetime as dt

import pytest

from custom_components.reolink_stamina.relevance.analysis import Analysis
from custom_components.reolink_stamina.relevance.journal import Journal, Transition

_CAMERA = "entry1:0"
_DAY = 86400.0
_NOW = 1_800_000_000.0


@pytest.fixture
async def journal(hass, tmp_path):
    """Return an open journal in a temporary file."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    yield store
    await store.async_close()


def _pair(at: float, kind: str = "person") -> list[Transition]:
    """Return the two transitions one detection produces."""
    entity = f"binary_sensor.drive_{kind}"
    return [
        Transition(camera=_CAMERA, entity_id=entity, kind=kind, state="on", at=at),
        Transition(camera=_CAMERA, entity_id=entity, kind=kind, state="off", at=at + 8.0),
    ]


async def test_a_model_is_built_from_what_the_journal_holds(hass, journal):
    """The whole chain: raw transitions in, merged events and learned rates out."""
    await journal.async_add([*_pair(1000.0), *_pair(90_000.0)])

    await Analysis(hass, journal).async_rebuild()
    analysis = Analysis(hass, journal)
    model = await analysis.async_rebuild()

    assert len(analysis.events) == 2
    assert model.per_camera[_CAMERA].events == 2


async def test_an_empty_journal_produces_an_empty_model(hass, journal):
    """A fresh install has nothing to learn from, and that is not an error."""
    model = await Analysis(hass, journal).async_rebuild()

    assert model.per_camera == {}


async def test_a_new_camera_is_still_collecting(hass, journal):
    """Nothing is scored until there is something to compare against."""
    await journal.async_add([*_pair(_NOW - 3600.0)])
    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()

    assert analysis.state(_CAMERA) == "collecting"
    assert analysis.coverage(_CAMERA)["events"] == 1


async def test_a_camera_nobody_has_added_reports_collecting(hass, journal):
    """The panel asks about every camera it lists, including ones with no history at all."""
    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()

    assert analysis.state("entry9:4") == "collecting"
    assert analysis.coverage("entry9:4") == {"days": 0.0, "events": 0}


async def test_months_of_days_with_few_events_says_so(hass, journal):
    """The third state, and the one that gets forgotten."""
    rows: list[Transition] = []
    for day in range(40):
        rows.extend(_pair(_NOW - (40 - day) * _DAY))
    await journal.async_add(rows)

    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()

    assert analysis.state(_CAMERA) == "too_few_events"


async def test_a_busy_watched_camera_becomes_active(hass, journal):
    """Enough days and enough events, so it can be compared with itself."""
    rows: list[Transition] = []
    for day in range(60):
        for hour in (7, 8, 18, 19):
            rows.extend(_pair(_NOW - (60 - day) * _DAY + hour * 3600))
    await journal.async_add(rows)

    analysis = Analysis(hass, journal)
    model = await analysis.async_rebuild()

    assert analysis.state(_CAMERA) == "active"
    assert model.thresholds.get(_CAMERA) is not None


async def test_scoring_a_window_walks_the_whole_history_for_context(hass, journal):
    """The predecessor of the first event in a window sits outside it."""
    rows: list[Transition] = []
    for day in range(60):
        for hour in (7, 8, 18, 19):
            rows.extend(_pair(_NOW - (60 - day) * _DAY + hour * 3600))
    await journal.async_add(rows)

    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()
    scored = await analysis.async_window(
        since=_NOW - 2 * _DAY, until=_NOW, names={_CAMERA: "Drive"}
    )

    assert scored
    assert len(scored) < len(analysis.events), "the window should be a slice, not everything"
    assert scored[0][0].started_at != analysis.events[0].started_at, (
        "context must come from outside the window"
    )
    assert all("Drive" in result.reason for _, result in scored)


async def test_a_window_sees_detections_recorded_since_the_last_rebuild(hass, journal):
    """The panel shows the journal as it stands, not as the nightly rebuild left it.

    A detection at teatime, or a signal switched on this morning, used to be invisible until
    03:17 the following night — which reads as the feature simply not working.
    """
    await journal.async_add(_pair(_NOW - 3 * _DAY))
    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()

    # Recorded after the model was built, exactly as a live detection is.
    await journal.async_add(_pair(_NOW - 60))

    scored = await analysis.async_window(since=_NOW - _DAY, until=_NOW + 1)
    assert [event.started_at for event, _ in scored] == [_NOW - 60]


async def test_rebuilding_picks_up_new_transitions(hass, journal):
    """Nightly, from scratch, so a changed constant reaches everything already collected."""
    await journal.async_add(_pair(1000.0))
    analysis = Analysis(hass, journal)
    await analysis.async_rebuild()
    assert len(analysis.events) == 1

    await journal.async_add(_pair(90_000.0))
    await analysis.async_rebuild()

    assert len(analysis.events) == 2


# ------------------------------------------------- which thread the nightly rebuild runs on


def _scheduled_action(track) -> object:
    """Return the action `async_schedule` registered, however it was passed."""
    call = track.call_args
    return call.kwargs["action"] if "action" in call.kwargs else call.args[1]


async def test_the_nightly_rebuild_is_scheduled_as_a_callback(hass, journal):
    """Home Assistant decides the thread from what the action *is*.

    A coroutine function is awaited, a `@callback` is called on the event loop, and anything
    else -- a plain lambda included -- is handed to an executor thread. The rebuild starts a
    task, and `hass.async_create_task` off the event loop is what `helpers.frame` reports:

        RuntimeError: Detected that custom integration 'reolink_stamina' calls
        hass.async_create_task from a thread other than the event loop

    It fired nightly at the rebuild hour and nowhere else, which is why nothing caught it.

    What was registered is asserted on twice: that it runs on the loop, and that running it
    still starts a rebuild. The first alone would be satisfied by a callback that had
    stopped doing anything at all.
    """
    from unittest.mock import AsyncMock, patch

    from homeassistant.core import is_callback

    analysis = Analysis(hass, journal)
    with patch(
        "custom_components.reolink_stamina.relevance.analysis.async_track_time_change"
    ) as track:
        analysis.async_schedule()

    action = _scheduled_action(track)
    assert is_callback(action), "the nightly rebuild would be run in an executor thread"

    with patch.object(analysis, "async_rebuild", AsyncMock()) as rebuild:
        action(dt.datetime(2026, 8, 4, 3, 17, tzinfo=dt.UTC))
        await hass.async_block_till_done()

    rebuild.assert_awaited_once()


async def test_scheduling_replaces_a_previous_registration(hass, journal):
    """Rescheduling must cancel the previous rebuild, not stack a second on one journal.

    Asserting that `_cancel` merely changed would not say this: reassigning the attribute
    is what a second registration does whether or not the first was ever called off. So the
    canceller the first registration handed back is the thing asserted on.
    """
    from unittest.mock import Mock, patch

    analysis = Analysis(hass, journal)
    with patch(
        "custom_components.reolink_stamina.relevance.analysis.async_track_time_change",
        side_effect=[Mock(name="first"), Mock(name="second")],
    ):
        analysis.async_schedule()
        first = analysis._cancel
        analysis.async_schedule()

    first.assert_called_once()

    analysis.async_cancel()
    assert analysis._cancel is None
