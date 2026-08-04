"""Tests for reading exact detection times out of the recorder."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.detections import (
    async_detection_entities,
    async_detections_in_window,
)

from .conftest import FakeApi, FakeHost

TZ = dt.timezone(dt.timedelta(hours=2))


def _reolink(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="reolink", title="NVR")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def _sensor(hass, entry, unique_id, object_id):
    return er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "reolink",
        unique_id,
        config_entry=entry,
        suggested_object_id=object_id,
    )


async def test_detection_sensors_are_found_by_channel(hass: HomeAssistant) -> None:
    """Only the requested camera's detection sensors, mapped to trigger names."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")
    _sensor(hass, entry, "MAC_1_pet", "cam1_pet")
    _sensor(hass, entry, "MAC_2_person", "cam2_person")
    _sensor(hass, entry, "MAC_1_sleep", "cam1_sleep")  # not a detection

    found = async_detection_entities(hass, entry.entry_id, 1)

    assert found == {
        "binary_sensor.cam1_person": "person",
        # Reolink calls it "pet"; the panel's vocabulary calls it animal.
        "binary_sensor.cam1_pet": "animal",
    }


async def test_smart_detection_zones_report_their_subject(hass: HomeAssistant) -> None:
    """Zone sensors are keyed by zone and subject, and it is the subject that counts.

    `crossline_dog_cat` used to resolve on its last segment, "cat", which matched nothing:
    an animal crossing a detection line produced no timeline marker and no uploaded clip.
    """
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_crossline_dog_cat", "gate_crossline_animal")
    _sensor(hass, entry, "MAC_1_intrusion_person", "gate_intrusion_person")
    _sensor(hass, entry, "MAC_1_linger_vehicle", "gate_linger_vehicle")
    _sensor(hass, entry, "MAC_1_non-motor_vehicle", "gate_bicycle")

    assert async_detection_entities(hass, entry.entry_id, 1) == {
        "binary_sensor.gate_crossline_animal": "animal",
        "binary_sensor.gate_intrusion_person": "person",
        "binary_sensor.gate_linger_vehicle": "vehicle",
        "binary_sensor.gate_bicycle": "vehicle",
    }


async def test_a_zone_reolink_has_not_invented_yet_still_reports_its_subject(
    hass: HomeAssistant,
) -> None:
    """Reolink adds detection zones regularly; an unknown one must not lose the subject."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_tripwire_person", "gate_tripwire_person")

    assert async_detection_entities(hass, entry.entry_id, 1) == {
        "binary_sensor.gate_tripwire_person": "person"
    }


async def test_channel_prefixed_sensors_are_found(hass: HomeAssistant) -> None:
    """Channels are also addressed as "ch1"."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_ch1_vehicle", "gate_vehicle")

    assert async_detection_entities(hass, entry.entry_id, 1) == {
        "binary_sensor.gate_vehicle": "vehicle"
    }


async def test_only_transitions_into_detected_are_reported(hass: HomeAssistant) -> None:
    """A sensor already on when the window opened says nothing about when it began."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")

    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)
    end = start + dt.timedelta(minutes=5)
    fired = start + dt.timedelta(seconds=142)

    def _states(*args, **kwargs):
        return {
            "binary_sensor.cam1_person": [
                State("binary_sensor.cam1_person", "off", last_changed=start),
                State("binary_sensor.cam1_person", "on", last_changed=fired),
                State(
                    "binary_sensor.cam1_person", "off", last_changed=fired + dt.timedelta(seconds=8)
                ),
            ]
        }

    with patch("custom_components.reolink_stamina.detections._async_history", side_effect=_states):
        found = await async_detections_in_window(hass, entry.entry_id, 1, start, end)

    assert len(found) == 1
    assert found[0]["kind"] == "person"
    # The offset is what the player seeks by.
    assert found[0]["offset"] == 142.0


async def test_a_detection_reports_when_it_cleared(hass: HomeAssistant) -> None:
    """The end is what lets the player trim a segment to the event instead of the clock."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")

    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)
    end = start + dt.timedelta(minutes=5)
    fired = start + dt.timedelta(seconds=60)

    def _states(*args, **kwargs):
        return {
            "binary_sensor.cam1_person": [
                State("binary_sensor.cam1_person", "off", last_changed=start),
                State("binary_sensor.cam1_person", "on", last_changed=fired),
                State(
                    "binary_sensor.cam1_person",
                    "off",
                    last_changed=fired + dt.timedelta(seconds=22),
                ),
            ]
        }

    with patch("custom_components.reolink_stamina.detections._async_history", side_effect=_states):
        found = await async_detections_in_window(hass, entry.entry_id, 1, start, end)

    assert len(found) == 1
    assert found[0]["offset"] == 60.0
    assert found[0]["end_offset"] == 82.0


async def test_a_detection_still_on_lasts_to_the_end_of_the_window(hass: HomeAssistant) -> None:
    """Claiming it stopped where the history ends would be a guess.

    The event may well continue into the next recording, so the clip is allowed to run to
    the end of this one rather than being cut short at the last state change.
    """
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")

    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)
    end = start + dt.timedelta(minutes=5)

    def _states(*args, **kwargs):
        return {
            "binary_sensor.cam1_person": [
                State("binary_sensor.cam1_person", "off", last_changed=start),
                State(
                    "binary_sensor.cam1_person",
                    "on",
                    last_changed=start + dt.timedelta(seconds=280),
                ),
            ]
        }

    with patch("custom_components.reolink_stamina.detections._async_history", side_effect=_states):
        found = await async_detections_in_window(hass, entry.entry_id, 1, start, end)

    assert found[0]["end_offset"] == 300.0


async def test_several_runs_of_one_sensor_are_reported_separately(hass: HomeAssistant) -> None:
    """Two people two minutes apart is two detections, and the clip spans both."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")

    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)
    end = start + dt.timedelta(minutes=5)

    def _at(seconds: int, state: str) -> State:
        return State(
            "binary_sensor.cam1_person", state, last_changed=start + dt.timedelta(seconds=seconds)
        )

    def _states(*args, **kwargs):
        return {
            "binary_sensor.cam1_person": [
                _at(0, "off"),
                _at(30, "on"),
                _at(45, "off"),
                _at(150, "on"),
                _at(170, "off"),
            ]
        }

    with patch("custom_components.reolink_stamina.detections._async_history", side_effect=_states):
        found = await async_detections_in_window(hass, entry.entry_id, 1, start, end)

    assert [(d["offset"], d["end_offset"]) for d in found] == [(30.0, 45.0), (150.0, 170.0)]


async def test_no_sensors_means_no_detections(hass: HomeAssistant) -> None:
    """Without detection sensors the player just starts at the beginning."""
    entry = _reolink(hass)
    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)

    found = await async_detections_in_window(
        hass, entry.entry_id, 1, start, start + dt.timedelta(minutes=5)
    )
    assert found == []


async def test_a_recorder_failure_is_not_fatal(hass: HomeAssistant) -> None:
    """A disabled or purged recorder must not break playback."""
    entry = _reolink(hass)
    _sensor(hass, entry, "MAC_1_person", "cam1_person")
    start = dt.datetime(2026, 8, 3, 9, 30, 0, tzinfo=TZ)

    with patch(
        "custom_components.reolink_stamina.detections._async_history",
        side_effect=RuntimeError("recorder is busy"),
    ):
        found = await async_detections_in_window(
            hass, entry.entry_id, 1, start, start + dt.timedelta(minutes=5)
        )

    assert found == []
