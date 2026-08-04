"""Tests for NVR discovery and the Reolink adapter.

Reaching the live reolink_aio object means reading `entry.runtime_data`, which is the
project's one use of non-public API — so every assumption
about its shape is pinned here. If upstream changes, these fail with a clear message
instead of the panel breaking at runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.nvr_registry import (
    NvrUnavailableError,
    ReolinkIncompatibleError,
    async_discover_nvrs,
    async_get_host,
    async_is_compatible,
)

from .conftest import FakeApi, FakeHost


def _reolink_entry(hass: HomeAssistant, runtime_data, *, loaded: bool = True):
    """Add a fake Reolink config entry in the requested state."""
    entry = MockConfigEntry(domain="reolink", title="Backyard NVR")
    entry.add_to_hass(hass)
    entry.runtime_data = runtime_data
    entry.mock_state(hass, ConfigEntryState.LOADED if loaded else ConfigEntryState.SETUP_RETRY)
    return entry


# ----------------------------------------------------------------- the adapter


async def test_get_host_returns_the_live_host(hass: HomeAssistant) -> None:
    """The happy path: a loaded entry hands back the reolink_aio host."""
    host = FakeHost(FakeApi())
    entry = _reolink_entry(hass, SimpleNamespace(host=host))
    assert async_get_host(hass, entry.entry_id) is host


async def test_get_host_rejects_an_unloaded_entry(hass: HomeAssistant) -> None:
    """A retrying entry has no usable API, and must not be treated as available."""
    entry = _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi())), loaded=False)
    with pytest.raises(NvrUnavailableError):
        async_get_host(hass, entry.entry_id)


async def test_get_host_rejects_an_unknown_entry(hass: HomeAssistant) -> None:
    """A stale entry id from a saved selection must not raise AttributeError."""
    with pytest.raises(NvrUnavailableError):
        async_get_host(hass, "does-not-exist")


async def test_get_host_detects_missing_runtime_data(hass: HomeAssistant) -> None:
    """Upstream dropping runtime_data is a compatibility problem, not a crash."""
    entry = _reolink_entry(hass, None)
    with pytest.raises(ReolinkIncompatibleError):
        async_get_host(hass, entry.entry_id)


async def test_get_host_detects_a_renamed_host_attribute(hass: HomeAssistant) -> None:
    """If runtime_data stops carrying `.host`, say so clearly."""
    entry = _reolink_entry(hass, SimpleNamespace(something_else=1))
    with pytest.raises(ReolinkIncompatibleError):
        async_get_host(hass, entry.entry_id)


async def test_get_host_detects_a_gutted_api(hass: HomeAssistant) -> None:
    """The API losing a method we depend on must be caught up front."""

    class Stunted:
        is_nvr = True

    entry = _reolink_entry(hass, SimpleNamespace(host=SimpleNamespace(api=Stunted())))
    with pytest.raises(ReolinkIncompatibleError):
        async_get_host(hass, entry.entry_id)


async def test_compatible_with_no_reolink_entries(hass: HomeAssistant) -> None:
    """No NVR set up yet is not an incompatibility; it must not raise a repair."""
    assert async_is_compatible(hass) is True


async def test_incompatible_when_every_loaded_entry_is_unreadable(
    hass: HomeAssistant,
) -> None:
    """This is what raises the repair issue."""
    _reolink_entry(hass, SimpleNamespace(nope=True))
    assert async_is_compatible(hass) is False


# ------------------------------------------------------------------- discovery


async def test_discovery_lists_an_nvr_and_its_cameras(hass: HomeAssistant) -> None:
    """The panel's NVR list, built entirely from the Reolink integration."""
    api = FakeApi(channels=[0, 1])
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(api)))

    nvrs = async_discover_nvrs(hass)
    assert len(nvrs) == 1
    nvr = nvrs[0]
    assert nvr.name == "Test NVR"
    assert nvr.status == "ok"
    assert nvr.connected is True
    assert nvr.has_storage is True
    assert [camera.channel for camera in nvr.cameras] == [0, 1]
    assert nvr.cameras[0].ai_types == ["person", "vehicle"]
    assert nvr.cameras[0].streams[:2] == ["sub", "main"]


async def test_discovery_skips_standalone_cameras(hass: HomeAssistant) -> None:
    """Standalone cameras are explicitly out of scope."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_nvr=False))))
    assert async_discover_nvrs(hass) == []


async def test_discovery_skips_hubs(hass: HomeAssistant) -> None:
    """Home Hubs record differently and are out of scope too."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_hub=True))))
    assert async_discover_nvrs(hass) == []


async def test_discovery_reports_unavailable_entries(hass: HomeAssistant) -> None:
    """A disconnected NVR is surfaced with a reason, not hidden."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi())), loaded=False)

    nvrs = async_discover_nvrs(hass)
    assert len(nvrs) == 1
    assert nvrs[0].status == "not_connected"
    assert nvrs[0].connected is False
    assert nvrs[0].cameras == []


async def test_discovery_reports_incompatible_entries(hass: HomeAssistant) -> None:
    """An unreadable entry is reported rather than silently dropped."""
    _reolink_entry(hass, SimpleNamespace(broken=True))
    nvrs = async_discover_nvrs(hass)
    assert [nvr.status for nvr in nvrs] == ["incompatible"]


async def test_discovery_flags_missing_storage(hass: HomeAssistant) -> None:
    """Without an HDD there is nothing to review, and the panel warns about it."""
    api = FakeApi()
    api.hdd_info = []
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(api)))
    assert async_discover_nvrs(hass)[0].has_storage is False


async def test_camera_playback_capability_is_reported(hass: HomeAssistant) -> None:
    """A channel that cannot replay is offered but marked unusable."""

    class NoReplay(FakeApi):
        def supported(self, channel, capability):
            return False

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(NoReplay())))
    nvr = async_discover_nvrs(hass)[0]
    assert all(camera.can_playback is False for camera in nvr.cameras)


async def test_camera_serialisation_shape(hass: HomeAssistant) -> None:
    """The websocket payload the panel depends on."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(channels=[0]))))
    data = async_discover_nvrs(hass)[0].as_dict()

    assert set(data) >= {
        "entry_id",
        "name",
        "status",
        "connected",
        "has_storage",
        "reports_triggers",
        "cameras",
    }
    assert set(data["cameras"][0]) >= {"channel", "name", "ai_types", "streams", "pre_record"}


class MinimalApi:
    """An NVR API exposing *only* what _REQUIRED_API_ATTRS declares.

    Mirrors an older reolink_aio: anything the code touches beyond the declared set
    raises AttributeError. This is the regression guard for exactly the bug that shipped
    in 0.1.0 — `api.is_dual_lens` was read without a guard and without being declared,
    so it worked against reolink_aio 0.21.8 and crashed against 0.21.4.
    """

    is_nvr = True
    is_hub = False
    channels: ClassVar[list[int]] = [0, 1]
    stream_channels: ClassVar[list[int]] = [0, 1]
    nvr_name = "Old NVR"
    hdd_info: ClassVar[list[dict[str, int]]] = [{"size": 1}]

    def camera_name(self, channel):
        return f"Cam {channel}"

    def supported(self, channel, capability):
        return capability == "replay"

    async def request_vod_files(self, *args, **kwargs):
        return [], []


async def test_discovery_works_with_only_the_declared_attributes(
    hass: HomeAssistant,
) -> None:
    """Discovery must not touch any API attribute it has not declared as required.

    Guards against depending on a newer reolink_aio than the user's Home Assistant pins.
    """
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(MinimalApi())))

    nvrs = async_discover_nvrs(hass)

    assert len(nvrs) == 1
    assert nvrs[0].status == "ok"
    assert nvrs[0].name == "Old NVR"
    assert [camera.name for camera in nvrs[0].cameras] == ["Cam 0", "Cam 1"]
    # Serialising must be safe too, since it is what crosses the websocket.
    assert nvrs[0].as_dict()["cameras"][0]["channel"] == 0


async def test_dual_lens_label_is_applied_when_available(hass: HomeAssistant) -> None:
    """Where the library does report dual lens, the label is still added."""

    class DualLens(MinimalApi):
        is_dual_lens = True

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(DualLens())))

    names = [camera.name for camera in async_discover_nvrs(hass)[0].cameras]
    assert names == ["Cam 0 (lens 0)", "Cam 1 (lens 1)"]
