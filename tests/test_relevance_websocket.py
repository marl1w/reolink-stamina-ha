"""What the panel is told about relevance.

The contract worth pinning down is that this answers *while a camera is still collecting*.
Scores mean nothing then and nothing is marked — but what was collected about each detection
does mean something, and showing it is the only way somebody who does not read Python can
tell whether the beta is working at all.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.reolink_stamina.const import CONF_BETA_RELEVANCE, DOMAIN
from custom_components.reolink_stamina.relevance.journal import Transition

from .conftest import FakeApi, FakeHost

_DAY = 86400.0


@pytest.fixture
async def setup_stamina(hass: HomeAssistant):
    """Set up Stamina with the relevance beta on, alongside a fake Reolink NVR."""
    assert await async_setup_component(hass, "http", {})

    api = FakeApi(channels=[0])
    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(api))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    entry = MockConfigEntry(
        domain=DOMAIN, title="Reolink Events", options={CONF_BETA_RELEVANCE: True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return SimpleNamespace(api=api, reolink=reolink, entry=entry)


async def _ask(hass, client, entry_id, *, days_back: float = 1.0):
    """Ask what is known about one camera over a window ending now."""
    now = dt_util.utcnow()
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/relevance",
            "entry_id": entry_id,
            "channel": 0,
            "start": (now - dt_util.dt.timedelta(days=days_back)).isoformat(),
            "end": now.isoformat(),
        }
    )
    return await client.receive_json()


async def test_it_says_so_plainly_when_the_beta_is_off(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
):
    """The panel must be able to tell "off" from "nothing to report"."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Reolink Events")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    response = await _ask(hass, client, "whatever")

    assert response["success"]
    assert response["result"] == {"enabled": False}


async def test_a_fresh_camera_reports_that_it_is_collecting(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
):
    """Which is what the panel shows instead of a mark nobody could trust yet."""
    client = await hass_ws_client(hass)
    response = await _ask(hass, client, setup_stamina.reolink.entry_id)

    assert response["success"]
    assert response["result"]["enabled"] is True
    assert response["result"]["state"] == "collecting"
    assert response["result"]["coverage"] == {"days": 0.0, "events": 0}


async def test_what_was_collected_is_returned_before_anything_can_be_scored(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
):
    """The point of the beta: useful on the first day, not only in a fortnight."""
    runtime = hass.data[DOMAIN].relevance
    camera = f"{setup_stamina.reolink.entry_id}:0"
    at = dt_util.utcnow().timestamp() - 3600.0
    await runtime.journal.async_add(
        [
            Transition(
                camera=camera,
                entity_id="binary_sensor.drive_person",
                kind="person",
                state="on",
                at=at,
            ),
            Transition(
                camera=camera,
                entity_id="binary_sensor.drive_person",
                kind="person",
                state="off",
                at=at + 8.0,
            ),
        ]
    )
    await runtime.analysis.async_rebuild()

    client = await hass_ws_client(hass)
    response = await _ask(hass, client, setup_stamina.reolink.entry_id)

    events = response["result"]["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "person"
    assert events[0]["duration"] == 8.0
    # Nothing may be marked while there is nothing to compare against.
    assert events[0]["threshold"] is None
    assert events[0]["unusual"] is False
    # But the terms are there, which is what the detail view shows.
    assert {term["name"] for term in events[0]["terms"]} >= {"clock", "duration", "predecessor"}
    assert events[0]["reason"]


async def test_a_camera_with_history_starts_marking(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
):
    """Enough behind it, and the odd one out is marked."""
    runtime = hass.data[DOMAIN].relevance
    camera = f"{setup_stamina.reolink.entry_id}:0"
    now = dt_util.utcnow().timestamp()

    rows: list[Transition] = []

    def add(at: float) -> None:
        rows.extend(
            [
                Transition(
                    camera=camera,
                    entity_id="binary_sensor.drive_person",
                    kind="person",
                    state=state,
                    at=at + offset,
                )
                for state, offset in (("on", 0.0), ("off", 8.0))
            ]
        )

    # Two months of a household that comes and goes at the same times.
    for day in range(60):
        base = now - (60 - day) * _DAY
        for hour in (7, 8, 18, 19):
            add(base + hour * 3600)
    add(now - 3600.0)

    await runtime.journal.async_add(rows)
    await runtime.analysis.async_rebuild()

    client = await hass_ws_client(hass)
    # The whole history, not a day of it. What the threshold promises is a share of the
    # distribution, and a handful of rows is far too small a sample to say anything about
    # that — a 25% assertion on five events fails on two, which means nothing either way.
    response = await _ask(hass, client, setup_stamina.reolink.entry_id, days_back=70)

    assert response["result"]["state"] == "active"
    events = response["result"]["events"]
    assert len(events) > 200
    assert all(item["threshold"] is not None for item in events)
    marked = sum(1 for item in events if item["unusual"])
    assert 0 < marked <= len(events) * 0.05
