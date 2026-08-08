"""The live subscription, and the one-off import of what the recorder still holds.

The watcher is what makes the journal independent of Home Assistant's recorder: from the
moment it is listening, a purge cannot take history away. These tests are about what it
writes down and — as importantly — what it declines to.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import patch

import pytest

from custom_components.reolink_stamina.const import (
    JOURNAL_BACKFILL_DAYS_DEFAULT,
    JOURNAL_BACKFILL_DAYS_MAX,
    JOURNAL_META_BACKFILLED,
)
from custom_components.reolink_stamina.relevance.backfill import (
    async_backfill,
    async_backfill_signals,
    async_retention_days,
)
from custom_components.reolink_stamina.relevance.journal import Journal, Transition
from custom_components.reolink_stamina.relevance.watcher import TransitionWatcher, async_signal_map

_SENSOR = "binary_sensor.drive_person"
_MAP = {_SENSOR: ("entry1:0", "person")}


@pytest.fixture
async def journal(hass, tmp_path):
    """Return an open journal in a temporary file."""
    store = Journal(hass, tmp_path / "journal.db")
    await store.async_open()
    yield store
    await store.async_close()


@pytest.fixture
def watcher(hass, journal):
    """Return a watcher already subscribed to one fake detection sensor."""
    with patch(
        "custom_components.reolink_stamina.relevance.watcher.async_detection_map",
        return_value=dict(_MAP),
    ):
        instance = TransitionWatcher(hass, journal)
        instance.async_start()
    yield instance
    instance.async_stop()


async def test_a_detection_is_written_down(hass, journal, watcher):
    """The whole point: a sensor firing becomes a row without the recorder's involvement."""
    hass.states.async_set(_SENSOR, "on")
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 1


async def test_clearing_is_recorded_too(hass, journal, watcher):
    """How long a detection lasted is a term in the score, so both ends have to be there."""
    hass.states.async_set(_SENSOR, "on")
    hass.states.async_set(_SENSOR, "off")
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 2


async def test_an_attribute_change_is_not_a_detection(hass, journal, watcher):
    """Reolink sensors carry attributes that update far more often than they fire."""
    hass.states.async_set(_SENSOR, "on")
    hass.states.async_set(_SENSOR, "on", {"last_seen": "later"})
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 1


async def test_unavailable_is_recorded(hass, journal, watcher):
    """Seeing nothing and not being connected must not look the same to a rate model."""
    hass.states.async_set(_SENSOR, "unavailable")
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 1


async def test_an_unknown_sensor_is_ignored(hass, journal, watcher):
    """Only the sensors resolved at start belong to a camera; anything else has no home."""
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 0


async def test_stopping_stops_recording(hass, journal, watcher):
    """A reload must not leave a listener writing into a journal that has been closed."""
    watcher.async_stop()
    hass.states.async_set(_SENSOR, "on")
    await hass.async_block_till_done()
    await journal.async_flush()

    assert (await journal.async_coverage())["transitions"] == 0
    assert watcher.watching == 0


async def test_finding_no_sensors_is_not_an_error(hass, journal):
    """The Reolink integration may not have finished setting up; the next reload tries again."""
    with patch(
        "custom_components.reolink_stamina.relevance.watcher.async_detection_map",
        return_value={},
    ):
        instance = TransitionWatcher(hass, journal)
        instance.async_start()

    assert instance.watching == 0


# ------------------------------------------------------------------ the import


def _states(hass, moments: list[tuple[str, float]]):
    """Build something shaped like what the recorder returns."""

    class _State:
        def __init__(self, state: str, at: float) -> None:
            self.entity_id = _SENSOR
            self.state = state
            self.last_changed = dt.datetime.fromtimestamp(at, dt.UTC)

    return {_SENSOR: [_State(state, at) for state, at in moments]}


async def test_the_import_writes_history_into_the_journal(hass, journal):
    """Enabling the feature should not mean a fortnight of waiting when history exists."""
    with (
        patch(
            "custom_components.reolink_stamina.relevance.backfill.async_detection_map",
            return_value=dict(_MAP),
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill.async_retention_days",
            return_value=1,
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            return_value=_states(hass, [("on", 1000.0), ("off", 1010.0)]),
        ),
    ):
        imported = await async_backfill(hass, journal)

    assert imported == 2
    assert await journal.async_get_meta(JOURNAL_META_BACKFILLED) is not None


async def test_the_import_does_not_run_twice(hass, journal):
    """A marker, so a restart does not pay for reading ninety days of history again."""
    await journal.async_set_meta(JOURNAL_META_BACKFILLED, "2026-08-08T00:00:00+00:00")

    with patch(
        "custom_components.reolink_stamina.relevance.backfill.async_detection_map",
        return_value=dict(_MAP),
    ) as resolved:
        assert await async_backfill(hass, journal) == 0

    resolved.assert_not_called()


async def test_the_import_is_not_marked_done_when_there_was_nothing_to_read(hass, journal):
    """Recorders that are not loaded yet must not cost the history for good."""
    with patch(
        "custom_components.reolink_stamina.relevance.backfill.async_detection_map",
        return_value={},
    ):
        assert await async_backfill(hass, journal) == 0

    assert await journal.async_get_meta(JOURNAL_META_BACKFILLED) is None


async def test_an_interrupted_import_can_be_repeated(hass, journal):
    """Chunks overlap by design, so a resumed import must add nothing already held.

    The marker is bypassed rather than removed: what is under test is the import doing the
    work twice, which is what a crash half way through leaves behind.
    """
    with (
        patch(
            "custom_components.reolink_stamina.relevance.backfill.async_detection_map",
            return_value=dict(_MAP),
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill.async_retention_days",
            return_value=1,
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            return_value=_states(hass, [("on", 1000.0), ("off", 1010.0)]),
        ),
        patch.object(journal, "async_get_meta", return_value=None),
    ):
        first = await async_backfill(hass, journal)
        second = await async_backfill(hass, journal)

    assert first == 2
    assert second == 0
    assert (await journal.async_coverage())["transitions"] == 2


async def test_retention_is_read_from_the_recorder_not_assumed(hass):
    """Somebody who set ninety days should get ninety, not the documented default."""

    class _Instance:
        keep_days = 45

    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=_Instance(),
    ):
        assert async_retention_days(hass) == 45


async def test_retention_is_bounded(hass):
    """A first import is not the place to walk years of history."""

    class _Instance:
        keep_days = 4000

    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=_Instance(),
    ):
        assert async_retention_days(hass) == JOURNAL_BACKFILL_DAYS_MAX


@pytest.mark.parametrize("keep_days", [None, 0, "nonsense"])
async def test_retention_falls_back_when_the_recorder_cannot_say(hass, keep_days):
    """An import that reaches less far is a smaller problem than one that refuses to run."""

    class _Instance:
        pass

    instance = _Instance()
    instance.keep_days = keep_days

    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        assert async_retention_days(hass) == JOURNAL_BACKFILL_DAYS_DEFAULT


# ------------------------------------------------ reconstructing signal history


class _Device:
    """Shaped like what the Reolink registry hands back."""

    def __init__(self, entry_id: str, channels: list[int]) -> None:
        self.entry_id = entry_id
        self.cameras = [type("Camera", (), {"channel": channel})() for channel in channels]


def _signal_states(entity_id: str, moments: list[tuple[str, float]]):
    class _State:
        def __init__(self, state: str, at: float) -> None:
            self.entity_id = entity_id
            self.state = state
            self.last_changed = dt.datetime.fromtimestamp(at, dt.UTC)

    return {entity_id: [_State(state, at) for state, at in moments]}


async def test_signals_are_reconstructed_onto_history_already_collected(hass, journal):
    """Adding a signal must not mean another week of waiting before it counts.

    Signals are snapshotted when a transition is written, so everything already in the journal
    carries none. Home Assistant has been recording the same entity all along, though, and
    reading it back is the difference between a signal that works today and one that works
    next Tuesday.
    """
    signal = "binary_sensor.anyone_home"
    await journal.async_add(
        [
            Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="on", at=1000.0),
            Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="off", at=1010.0),
            Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="on", at=5000.0),
            Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="off", at=5010.0),
        ]
    )

    with (
        patch(
            "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
            return_value=[_Device("entry1", [0])],
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            # Somebody went out between the two detections.
            return_value=_signal_states(signal, [("on", 900.0), ("off", 3000.0)]),
        ),
    ):
        stamped = await async_backfill_signals(hass, journal, {"entry1": [signal]})

    assert stamped == 4
    rows = await journal.async_transitions()
    assert [json.loads(row.context)[signal] for row in rows] == ["on", "on", "off", "off"]


async def test_reconstructing_signals_does_not_repeat_itself(hass, journal):
    """A reload must be free, or every restart re-reads and rewrites the whole journal."""
    signal = "binary_sensor.anyone_home"
    await journal.async_add(
        [Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="on", at=1000.0)]
    )

    def _run():
        return patch(
            "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
            return_value=[_Device("entry1", [0])],
        ), patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            return_value=_signal_states(signal, [("on", 900.0)]),
        )

    first, second = _run()
    with first, second:
        assert await async_backfill_signals(hass, journal, {"entry1": [signal]}) == 1
    first, second = _run()
    with first, second:
        assert await async_backfill_signals(hass, journal, {"entry1": [signal]}) == 0


async def test_adding_a_signal_restamps_all_of_them(hass, journal):
    """A snapshot missing an entity is not the same fact as one recording it as absent."""
    await journal.async_add(
        [Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="on", at=1000.0)]
    )
    devices = patch(
        "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
        return_value=[_Device("entry1", [0])],
    )

    with (
        devices,
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            return_value=_signal_states("binary_sensor.one", [("on", 900.0)]),
        ),
    ):
        await async_backfill_signals(hass, journal, {"entry1": ["binary_sensor.one"]})

    with (
        patch(
            "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
            return_value=[_Device("entry1", [0])],
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            return_value={
                **_signal_states("binary_sensor.one", [("on", 900.0)]),
                **_signal_states("binary_sensor.two", [("off", 900.0)]),
            },
        ),
    ):
        stamped = await async_backfill_signals(
            hass, journal, {"entry1": ["binary_sensor.one", "binary_sensor.two"]}
        )

    assert stamped == 1, "the row already stamped is stamped again, with both signals"
    rows = await journal.async_transitions()
    assert json.loads(rows[0].context) == {"binary_sensor.one": "on", "binary_sensor.two": "off"}


async def test_a_signal_older_than_the_history_reads_as_unknown(hass, journal):
    """A hole would silently shift every count that mentions it; `unknown` is a state."""
    await journal.async_add(
        [Transition(camera="entry1:0", entity_id=_SENSOR, kind="person", state="on", at=1000.0)]
    )

    with (
        patch(
            "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
            return_value=[_Device("entry1", [0])],
        ),
        patch(
            "custom_components.reolink_stamina.relevance.backfill._async_history",
            # The recorder only reaches back to well after the detection.
            return_value=_signal_states("binary_sensor.late", [("on", 9000.0)]),
        ),
    ):
        await async_backfill_signals(hass, journal, {"entry1": ["binary_sensor.late"]})

    rows = await journal.async_transitions()
    assert json.loads(rows[0].context) == {"binary_sensor.late": "unknown"}


async def test_a_cameras_own_signals_go_to_that_camera_and_no_other(hass, journal):
    """Thirteen cameras must not each carry thirteen day/night sensors.

    A floodlight belongs to one lens. What the user picks is per recorder, because "is anybody
    home" is true of the property; what the camera reports about itself is per camera, and is
    discovered rather than chosen — the same way its detection sensors are.
    """
    own = {
        ("entry1", 0): ["light.drive_floodlight", "sensor.drive_day_night_state"],
        ("entry1", 1): ["light.gate_floodlight", "sensor.gate_day_night_state"],
    }

    with (
        patch(
            "custom_components.reolink_stamina.relevance.watcher.async_discover_devices",
            return_value=[_Device("entry1", [0, 1])],
        ),
        patch(
            "custom_components.reolink_stamina.relevance.watcher.async_camera_signal_entities",
            side_effect=lambda _hass, entry_id, channel: own[(entry_id, channel)],
        ),
    ):
        found = async_signal_map(hass, {"entry1": ["person.someone"]})

    assert found == {
        "entry1:0": [
            "light.drive_floodlight",
            "person.someone",
            "sensor.drive_day_night_state",
        ],
        "entry1:1": [
            "light.gate_floodlight",
            "person.someone",
            "sensor.gate_day_night_state",
        ],
    }
