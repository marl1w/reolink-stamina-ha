"""Whether the journal is running, which comes down to whether the user asked for it.

The important assertion here is the negative one. Off is the default and off has to mean
that no file exists at all — not an empty database, not a table with no rows. A behavioural
record of a household that appears because somebody installed a video panel is the thing
this design is most careful to avoid, and it would be invisible in every other test.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import (
    CONF_BETA_RELEVANCE,
    DOMAIN,
    JOURNAL_FILENAME,
)

from .conftest import FakeApi, FakeHost


def _loaded_reolink(hass: HomeAssistant) -> MockConfigEntry:
    """Add a loaded Reolink entry holding one NVR."""
    entry = MockConfigEntry(domain="reolink", title="Backyard NVR")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def _setup(hass: HomeAssistant, *, relevance: bool) -> MockConfigEntry:
    """Set up the integration with the Relevance beta on or off."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Reolink Events",
        options={CONF_BETA_RELEVANCE: relevance},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _journal_path(hass: HomeAssistant) -> Path:
    """Return where the journal would live."""
    return Path(hass.config.path(JOURNAL_FILENAME))


async def test_nothing_is_recorded_while_the_beta_is_off(hass: HomeAssistant) -> None:
    """Off means no file, not an empty one."""
    _loaded_reolink(hass)
    await _setup(hass, relevance=False)

    assert hass.data[DOMAIN].relevance is None
    assert not _journal_path(hass).exists()


async def test_turning_it_on_opens_the_journal(hass: HomeAssistant) -> None:
    """The opt-in is what creates the record."""
    _loaded_reolink(hass)
    await _setup(hass, relevance=True)

    runtime = hass.data[DOMAIN].relevance
    assert runtime is not None
    assert _journal_path(hass).exists()


async def test_unloading_closes_the_journal(hass: HomeAssistant) -> None:
    """A reload must not leave a listener writing into a database nobody owns."""
    _loaded_reolink(hass)
    entry = await _setup(hass, relevance=True)
    runtime = hass.data[DOMAIN].relevance

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert runtime.watcher.watching == 0


async def test_reloading_with_the_beta_on_survives(hass: HomeAssistant) -> None:
    """Opening the same file twice in one process is the reload path."""
    _loaded_reolink(hass)
    entry = await _setup(hass, relevance=True)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN].relevance is not None


async def test_removing_the_integration_deletes_the_record(hass: HomeAssistant) -> None:
    """What was collected should not outlive the decision to remove what collected it."""
    _loaded_reolink(hass)
    entry = await _setup(hass, relevance=True)
    assert _journal_path(hass).exists()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not _journal_path(hass).exists()
