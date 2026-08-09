"""Whether the journal is running, which it is from the moment the panel is set up.

Counting used to be opt-in, and the assertion that mattered was the negative one: with it
off, no file at all. There is nothing to switch on any more — a setup with six decisions in
it is a setup most people get wrong — so the guarantee that remains is the one at the other
end. What was collected must not outlive the decision to remove what collected it, and that
is the assertion this file exists for.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import DOMAIN, JOURNAL_FILENAME

from .conftest import FakeApi, FakeHost


def _loaded_reolink(hass: HomeAssistant) -> MockConfigEntry:
    """Add a loaded Reolink entry holding one NVR."""
    entry = MockConfigEntry(domain="reolink", title="Backyard NVR")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Reolink Events")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _journal_path(hass: HomeAssistant) -> Path:
    """Return where the journal would live."""
    return Path(hass.config.path(JOURNAL_FILENAME))


async def test_setting_up_opens_the_journal(hass: HomeAssistant) -> None:
    """Setting the panel up is what creates the record."""
    _loaded_reolink(hass)
    await _setup(hass)

    runtime = hass.data[DOMAIN].relevance
    assert runtime is not None
    assert _journal_path(hass).exists()


async def test_unloading_closes_the_journal(hass: HomeAssistant) -> None:
    """A reload must not leave a listener writing into a database nobody owns."""
    _loaded_reolink(hass)
    entry = await _setup(hass)
    runtime = hass.data[DOMAIN].relevance

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert runtime.watcher.watching == 0


async def test_reloading_survives(hass: HomeAssistant) -> None:
    """Opening the same file twice in one process is the reload path."""
    _loaded_reolink(hass)
    entry = await _setup(hass)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN].relevance is not None


async def test_removing_the_integration_deletes_the_record(hass: HomeAssistant) -> None:
    """What was collected should not outlive the decision to remove what collected it."""
    _loaded_reolink(hass)
    entry = await _setup(hass)
    assert _journal_path(hass).exists()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not _journal_path(hass).exists()
