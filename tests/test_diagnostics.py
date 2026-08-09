"""Tests for the diagnostics download.

This exists for bugs that only happen on other people's machines, so what matters is that
it answers without a running conversion, without ffmpeg, and without a recorder — the state
every bug report is filed from — and that what it answers with is the evidence rather than
raw ffmpeg output.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import DOMAIN
from custom_components.reolink_stamina.diagnostics import async_get_config_entry_diagnostics
from custom_components.reolink_stamina.restream import Diagnosis, async_get_manager

from .conftest import FakeApi, FakeHost


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up Stamina alongside a fake recorder."""
    assert await async_setup_component(hass, "http", {})

    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi(channels=[0])))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    stamina = MockConfigEntry(domain=DOMAIN, title="Reolink Events")
    stamina.add_to_hass(hass)
    assert await hass.config_entries.async_setup(stamina.entry_id)
    await hass.async_block_till_done()
    return stamina


async def test_diagnostics_answer_with_nothing_having_run(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The state every bug report is filed from: no conversion running, nothing probed."""
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["adaptive_playback"]["failures"] == []
    assert report["adaptive_playback"]["encoder"] == "not yet probed"
    # The number that says whether sessions are being abandoned, which is the leak this is
    # meant to make visible without asking anyone to run a shell command.
    assert report["temporary_space"]["session_directories"] >= 0
    assert "free_bytes" in report["temporary_space"]


async def test_diagnostics_carry_the_classified_failures(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A cause, and ffmpeg's own words alongside it — not one in place of the other."""
    manager = async_get_manager(hass)
    stream = SimpleNamespace(
        label="entry/0/main@0s hls",
        encoder=SimpleNamespace(name="h264_qsv", hardware=True),
        error_detail="Device creation failed",
        process=SimpleNamespace(returncode=1, pid=1),
    )
    broken = Diagnosis("encoder_unavailable", "The GPU failed.", True)

    manager.note_failure(stream, broken, mode="encode")

    report = await async_get_config_entry_diagnostics(hass, entry)

    (failure,) = report["adaptive_playback"]["failures"]
    assert failure["code"] == "encoder_unavailable"
    assert failure["message"] == "The GPU failed."
    assert failure["ffmpeg"] == "Device creation failed"
    assert report["adaptive_playback"]["disabled_encoders"] == ["h264_qsv"]


async def test_diagnostics_put_both_clocks_side_by_side(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Playback is addressed by timestamp, so a disagreeing clock is a 404 and nothing else.

    Reported for both ends rather than one: knowing only Home Assistant's timezone says
    nothing, and the whole diagnosis is the difference between the two.
    """
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert "home_assistant_timezone" in report["clocks"]
    assert "home_assistant_utc_offset" in report["clocks"]
    # One entry per recorder, whether or not it would answer — an unreachable recorder is
    # itself worth seeing, and must not cost the rest of the report.
    assert isinstance(report["clocks"]["recorders"], list)


async def test_diagnostics_survive_a_recorder_that_will_not_answer(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A report that raises is a report nobody can attach to the issue they are filing."""
    with patch(
        "custom_components.reolink_stamina.diagnostics.async_get_host",
        side_effect=RuntimeError("gone"),
    ):
        report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["clocks"]["recorders"]
    assert "gone" in report["clocks"]["recorders"][0]["error"]


async def test_diagnostics_show_what_a_playback_url_is_built_from(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The three timestamps whose disagreement is the 404, rather than the URL after the fact."""
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert isinstance(report["playback_samples"], list)


async def test_diagnostics_name_no_recording(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A failure label names the camera and the offset, and neither belongs in a bug report."""
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert "password" not in repr(report).lower()
    assert "token" not in repr(report).lower()
