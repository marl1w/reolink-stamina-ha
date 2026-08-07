"""Tests for the diagnostics download.

This exists for bugs that only happen on other people's machines, so what matters is that
it answers without a running conversion, without ffmpeg, and without a recorder — the state
every bug report is filed from — and that what it answers with is the evidence rather than
raw ffmpeg output.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import CONF_BETA_RESTREAM, DOMAIN
from custom_components.reolink_stamina.diagnostics import async_get_config_entry_diagnostics
from custom_components.reolink_stamina.restream import Diagnosis, async_get_manager

from .conftest import FakeApi, FakeHost


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up Stamina with the beta on, alongside a fake recorder."""
    assert await async_setup_component(hass, "http", {})

    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi(channels=[0])))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    stamina = MockConfigEntry(
        domain=DOMAIN, title="Reolink Events", options={CONF_BETA_RESTREAM: True}
    )
    stamina.add_to_hass(hass)
    assert await hass.config_entries.async_setup(stamina.entry_id)
    await hass.async_block_till_done()
    return stamina


async def test_diagnostics_answer_with_nothing_having_run(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The state every bug report is filed from: no conversion running, nothing probed."""
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["adaptive_playback"]["enabled"] is True
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


async def test_diagnostics_name_no_recording(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A failure label names the camera and the offset, and neither belongs in a bug report."""
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert "password" not in repr(report).lower()
    assert "token" not in repr(report).lower()
