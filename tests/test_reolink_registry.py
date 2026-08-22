"""Tests for device discovery and the Reolink adapter.

Reaching the live reolink_aio object means reading `entry.runtime_data`, which is the
project's one use of non-public API — so every assumption
about its shape is pinned here. If upstream changes, these fail with a clear message
instead of the panel breaking at runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.reolink_registry import (
    EXCLUDED_DISABLED,
    EXCLUDED_NOT_A_RECORDER,
    DeviceUnavailableError,
    ReolinkIncompatibleError,
    async_discover,
    async_discover_devices,
    async_get_host,
    async_is_compatible,
)

from .conftest import FakeApi, FakeHost


def _reolink_entry(
    hass: HomeAssistant, runtime_data, *, loaded: bool = True, disabled: bool = False
):
    """Add a fake Reolink config entry in the requested state."""
    entry = MockConfigEntry(
        domain="reolink",
        title="Backyard NVR",
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = runtime_data
    if disabled:
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    else:
        entry.mock_state(hass, ConfigEntryState.LOADED if loaded else ConfigEntryState.SETUP_RETRY)
    return entry


def _reolink_channel_device(
    hass: HomeAssistant, entry, channel: int, *, name: str, disabled: bool = False
):
    """Register the device and camera entity the Reolink integration creates for a channel.

    The unique_id shape is Reolink's own (`<host uid>_ch<n>_<stream>`), because that is
    what discovery parses to tie an entity back to a channel.
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("reolink", f"NVRUID_ch{channel}")},
        name=name,
    )
    er.async_get(hass).async_get_or_create(
        "camera",
        "reolink",
        f"NVRUID_ch{channel}_sub",
        config_entry=entry,
        device_id=device.id,
    )
    if disabled:
        device = dev_reg.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    return device


# ----------------------------------------------------------------- the adapter


async def test_get_host_returns_the_live_host(hass: HomeAssistant) -> None:
    """The happy path: a loaded entry hands back the reolink_aio host."""
    host = FakeHost(FakeApi())
    entry = _reolink_entry(hass, SimpleNamespace(host=host))
    assert async_get_host(hass, entry.entry_id) is host


async def test_get_host_rejects_an_unloaded_entry(hass: HomeAssistant) -> None:
    """A retrying entry has no usable API, and must not be treated as available."""
    entry = _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi())), loaded=False)
    with pytest.raises(DeviceUnavailableError):
        async_get_host(hass, entry.entry_id)


async def test_get_host_rejects_an_unknown_entry(hass: HomeAssistant) -> None:
    """A stale entry id from a saved selection must not raise AttributeError."""
    with pytest.raises(DeviceUnavailableError):
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

    devices = async_discover_devices(hass)
    assert len(devices) == 1
    device = devices[0]
    assert device.name == "Test NVR"
    assert device.status == "ok"
    assert device.connected is True
    assert device.has_storage is True
    assert [camera.channel for camera in device.cameras] == [0, 1]
    assert device.cameras[0].ai_types == ["person", "vehicle"]
    assert device.cameras[0].streams[:2] == ["sub", "main"]


async def test_discovery_skips_standalone_cameras(hass: HomeAssistant) -> None:
    """Standalone cameras are explicitly out of scope."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_nvr=False))))
    assert async_discover_devices(hass) == []


async def test_discovery_skips_hubs(hass: HomeAssistant) -> None:
    """Home Hubs record differently and are out of scope too."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_hub=True))))
    assert async_discover_devices(hass) == []


async def test_discovery_reports_unavailable_entries(hass: HomeAssistant) -> None:
    """A disconnected NVR is surfaced with a reason, not hidden."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi())), loaded=False)

    devices = async_discover_devices(hass)
    assert len(devices) == 1
    assert devices[0].status == "not_connected"
    assert devices[0].connected is False
    assert devices[0].cameras == []


async def test_discovery_reports_incompatible_entries(hass: HomeAssistant) -> None:
    """An unreadable entry is reported rather than silently dropped."""
    _reolink_entry(hass, SimpleNamespace(broken=True))
    devices = async_discover_devices(hass)
    assert [device.status for device in devices] == ["incompatible"]


async def test_discovery_flags_missing_storage(hass: HomeAssistant) -> None:
    """Without an HDD there is nothing to review, and the panel warns about it."""
    api = FakeApi()
    api.hdd_info = []
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(api)))
    assert async_discover_devices(hass)[0].has_storage is False


async def test_camera_playback_capability_is_reported(hass: HomeAssistant) -> None:
    """A channel that cannot replay is offered but marked unusable."""

    class NoReplay(FakeApi):
        def supported(self, channel, capability):
            return False

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(NoReplay())))
    device = async_discover_devices(hass)[0]
    assert all(camera.can_playback is False for camera in device.cameras)


async def test_camera_serialisation_shape(hass: HomeAssistant) -> None:
    """The websocket payload the panel depends on."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(channels=[0]))))
    data = async_discover_devices(hass)[0].as_dict()

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

    devices = async_discover_devices(hass)

    assert len(devices) == 1
    assert devices[0].status == "ok"
    assert devices[0].name == "Old NVR"
    assert [camera.name for camera in devices[0].cameras] == ["Cam 0", "Cam 1"]
    # Serialising must be safe too, since it is what crosses the websocket.
    assert devices[0].as_dict()["cameras"][0]["channel"] == 0


async def test_dual_lens_label_is_applied_when_available(hass: HomeAssistant) -> None:
    """Where the library does report dual lens, the label is still added."""

    class DualLens(MinimalApi):
        is_dual_lens = True

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(DualLens())))

    names = [camera.name for camera in async_discover_devices(hass)[0].cameras]
    assert names == ["Cam 0 (lens 0)", "Cam 1 (lens 1)"]


# ------------------------------------------------ hubs and standalone cameras


async def test_hubs_and_cameras_are_listed_only_when_asked_for(hass: HomeAssistant) -> None:
    """The panel asks for everything; cloud sync and the DHCP suggestion do not."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_hub=True))))

    assert async_discover_devices(hass) == []

    devices = async_discover_devices(hass, include_all_devices=True)
    assert [device.kind for device in devices] == ["hub"]
    assert devices[0].status == "ok"
    assert devices[0].cameras, "a hub's own channels are still cameras to browse"


async def test_a_standalone_camera_says_what_it_is(hass: HomeAssistant) -> None:
    """The panel labels it, because nothing here has been tested against one."""
    api = FakeApi(is_nvr=False, channels=[0])
    api.nvr_name = "Doorbell"
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(api)))

    devices = async_discover_devices(hass, include_all_devices=True)

    assert [device.kind for device in devices] == ["camera"]
    assert devices[0].name == "Doorbell"
    assert devices[0].as_dict()["kind"] == "camera"


async def test_a_camera_already_on_an_nvr_is_not_listed_twice(hass: HomeAssistant) -> None:
    """The same footage under two names is worse than one name missing.

    A camera on an NVR is very often also set up on its own in the Reolink integration, and
    the UID is what matches the two: it is Reolink's own identity for a camera and what the
    Reolink integration keys its entities on.
    """

    class Recorder(FakeApi):
        def camera_uid(self, channel):
            return f"CAMUID{channel}"

    class Camera(FakeApi):
        uid = "CAMUID1"

        def camera_uid(self, channel):
            return self.uid

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Recorder(channels=[0, 1]))))
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Camera(is_nvr=False, channels=[0]))))

    devices = async_discover_devices(hass, include_all_devices=True)

    assert [device.kind for device in devices] == ["nvr"]


async def test_a_camera_on_no_recorder_is_still_listed(hass: HomeAssistant) -> None:
    """Deduplication must not become a reason to hide a camera on no recorder."""

    class Recorder(FakeApi):
        def camera_uid(self, channel):
            return f"CAMUID{channel}"

    class Camera(FakeApi):
        uid = "SOMETHINGELSE"

        def camera_uid(self, channel):
            return self.uid

    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Recorder(channels=[0, 1]))))
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Camera(is_nvr=False, channels=[0]))))

    devices = async_discover_devices(hass, include_all_devices=True)

    assert sorted(device.kind for device in devices) == ["camera", "nvr"]


# --------------------------------------------- what Home Assistant has been told to hide


async def test_an_entry_disabled_in_home_assistant_is_left_out(hass: HomeAssistant) -> None:
    """Disabling a Reolink entry is a decision, not a fault to report back.

    Issue #4: eight disabled camera entries were carded as unavailable -- the status check
    runs before the recorders-only filter, so they appeared while the one working camera
    was filtered out. That reads as "standalone cameras work and mine is broken".
    """
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_nvr=False))), disabled=True)

    found = async_discover(hass)

    assert found.devices == []
    assert [item.reason for item in found.excluded] == [EXCLUDED_DISABLED]


async def test_exclusions_say_why(hass: HomeAssistant) -> None:
    """An entry that is nowhere in the panel has to be somewhere in diagnostics."""
    entry = _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(is_nvr=False))))

    excluded = async_discover(hass).excluded

    assert [(item.entry_id, item.reason, item.kind) for item in excluded] == [
        (entry.entry_id, EXCLUDED_NOT_A_RECORDER, "camera")
    ]
    assert async_discover(hass, include_all_devices=True).excluded == []


async def test_a_channel_disabled_in_home_assistant_is_not_offered(hass: HomeAssistant) -> None:
    """A camera the user disabled on the recorder must not come back through this panel."""
    entry = _reolink_entry(hass, SimpleNamespace(host=FakeHost(FakeApi(channels=[0, 1]))))
    _reolink_channel_device(hass, entry, 0, name="Driveway")
    _reolink_channel_device(hass, entry, 1, name="Doorbell NVR", disabled=True)

    cameras = async_discover_devices(hass)[0].cameras

    assert [(camera.channel, camera.name) for camera in cameras] == [(0, "Driveway")]


async def test_a_disabled_channel_does_not_hide_the_direct_camera(hass: HomeAssistant) -> None:
    """The dedup must not keep the copy the user disabled and drop the one they kept.

    Issue #4's setup: one doorbell, reachable through the NVR and on its own direct
    connection, with the NVR's copy disabled in Home Assistant.
    """

    class Recorder(FakeApi):
        def camera_uid(self, channel):
            return f"CAMUID{channel}"

    class Doorbell(FakeApi):
        uid = "CAMUID1"

        def camera_uid(self, channel):
            return self.uid

    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR", disabled=True)
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Doorbell(is_nvr=False, channels=[0]))))

    found = async_discover(hass, include_all_devices=True)

    assert sorted(device.kind for device in found.devices) == ["camera", "nvr"]
    assert found.excluded == []

    # And the copy they kept now knows where the recordings actually are. The doorbell
    # writes to the recorder, so its own card is empty and the disabled channel is the
    # only place its footage exists.
    doorbell = next(device for device in found.devices if device.kind == "camera")
    assert doorbell.cameras[0].paired_entry_id == recorder.entry_id
    assert doorbell.cameras[0].paired_channel == 1


async def test_an_enabled_channel_still_hides_the_direct_camera(hass: HomeAssistant) -> None:
    """The dedup itself is unchanged: a channel in use is still the one that wins."""

    class Recorder(FakeApi):
        def camera_uid(self, channel):
            return f"CAMUID{channel}"

    class Doorbell(FakeApi):
        uid = "CAMUID1"

        def camera_uid(self, channel):
            return self.uid

    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR")
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Doorbell(is_nvr=False, channels=[0]))))

    assert [device.kind for device in async_discover_devices(hass, include_all_devices=True)] == [
        "nvr"
    ]


async def test_deduplication_survives_a_library_without_camera_uid(hass: HomeAssistant) -> None:
    """An older reolink_aio costs the duplicate check, not the whole device list."""
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(MinimalApi())))
    camera = MinimalApi()
    camera.is_nvr = False
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(camera)))

    devices = async_discover_devices(hass, include_all_devices=True)

    assert sorted(device.kind for device in devices) == ["camera", "nvr"]


# ----------------------------------------------------- pairing a camera to its disabled channel


class _Recorder(FakeApi):
    """A recorder that files each channel under Reolink's own UID for the camera on it."""

    def camera_uid(self, channel):
        return f"CAMUID{channel}"


class _Doorbell(FakeApi):
    """A directly-connected camera, which is its own host and so answers with its own UID."""

    uid = "CAMUID1"

    def camera_uid(self, channel):
        return self.uid


async def test_an_enabled_channel_pairs_nothing(hass: HomeAssistant) -> None:
    """There is nothing to pair when the recorder's own copy is the one being watched.

    The direct entry is deduplicated away entirely in that case, so a pairing would have
    nothing to hang off. Pairing exists for the setup where the *disabled* copy is where
    the recordings are.
    """
    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR")
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Doorbell(is_nvr=False, channels=[0]))))

    devices = async_discover_devices(hass, include_all_devices=True)

    assert [device.kind for device in devices] == ["nvr"]
    assert all(camera.paired_entry_id is None for camera in devices[0].cameras)


async def test_a_camera_on_no_recorder_is_not_paired(hass: HomeAssistant) -> None:
    """A camera that simply is not on a recorder must not be pointed at one."""

    class Stranger(FakeApi):
        uid = "SOMETHINGELSE"

        def camera_uid(self, channel):
            return self.uid

    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR", disabled=True)
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(Stranger(is_nvr=False, channels=[0]))))

    devices = async_discover_devices(hass, include_all_devices=True)
    camera = next(device for device in devices if device.kind == "camera")

    assert camera.cameras[0].paired_entry_id is None
    assert camera.cameras[0].paired_channel is None


async def test_a_recorders_own_channel_is_never_paired_away(hass: HomeAssistant) -> None:
    """A recorder's channel is where a pairing points *to*, never where it points from.

    One camera on two recorders, kept on the first and disabled on the second, is the case
    that makes this bite: the same UID is then both deduplicating (from the recorder watching
    it) and pairable (from the one that is not), so the two halves of the index are *not*
    disjoint across recorders. Without the check, the channel actually being watched would be
    pointed at the copy the user switched off and searched twice for one camera.
    """
    watching = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, watching, 1, name="Doorbell NVR")
    ignoring = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, ignoring, 1, name="Doorbell spare", disabled=True)

    devices = async_discover_devices(hass, include_all_devices=True)
    channels = [camera for device in devices if device.kind == "nvr" for camera in device.cameras]

    assert channels, "the recorders' channels should still be listed"
    assert all(camera.paired_entry_id is None for camera in channels)


async def test_pairing_survives_a_library_without_camera_uid(hass: HomeAssistant) -> None:
    """An older reolink_aio costs the pairing, not the camera.

    `camera_uid` is not in every version, and neither is `uid`. Losing the pairing puts the
    camera back exactly where it is today -- listed, searched against its own storage -- which
    is a worse answer than the pairing and a far better one than no camera at all.
    """

    class NoUids(FakeApi):
        """A camera whose library exposes neither UID read."""

    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR", disabled=True)
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(NoUids(is_nvr=False, channels=[0]))))

    devices = async_discover_devices(hass, include_all_devices=True)
    camera = next(device for device in devices if device.kind == "camera")

    assert camera.cameras[0].paired_entry_id is None


async def test_the_pairing_is_serialised_for_the_panel(hass: HomeAssistant) -> None:
    """The row marker is drawn from this, so it has to survive the websocket."""
    recorder = _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Recorder(channels=[0, 1]))))
    _reolink_channel_device(hass, recorder, 1, name="Doorbell NVR", disabled=True)
    _reolink_entry(hass, SimpleNamespace(host=FakeHost(_Doorbell(is_nvr=False, channels=[0]))))

    devices = async_discover(hass, include_all_devices=True).devices
    camera = next(device for device in devices if device.kind == "camera").as_dict()

    assert camera["cameras"][0]["paired_entry_id"] == recorder.entry_id
    assert camera["cameras"][0]["paired_channel"] == 1
