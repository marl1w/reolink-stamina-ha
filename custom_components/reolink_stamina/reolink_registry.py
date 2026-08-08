"""Discovery of recording Reolink devices via the official Reolink integration.

This is the *only* module allowed to reach into the Reolink integration's internals.
Everything else in this integration goes through the dataclasses returned here, so that
an upstream change breaks one file with a clear error rather than the whole panel.

This is the project's one dependency on non-public API, so it is kept in a single place
and pinned by tests/test_upstream_contract.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import REOLINK_DOMAIN, STREAM_MAIN, STREAM_SUB

_LOGGER = logging.getLogger(__name__)

# Attributes we rely on. Checked up front so an upstream rename is reported as a
# compatibility problem instead of surfacing as an AttributeError mid-search.
_REQUIRED_API_ATTRS = (
    "is_nvr",
    "is_hub",
    "channels",
    "stream_channels",
    "camera_name",
    "nvr_name",
    "supported",
    "request_vod_files",
    "hdd_info",
)


class ReolinkIncompatibleError(HomeAssistantError):
    """Raised when the installed Reolink integration is not shaped as expected."""


class DeviceUnavailableError(HomeAssistantError):
    """Raised when a requested device is not currently usable."""


@dataclass(slots=True)
class CameraInfo:
    """A single camera channel on a recording device."""

    channel: int
    name: str
    ai_types: list[str] = field(default_factory=list)
    streams: list[str] = field(default_factory=list)
    can_playback: bool = True
    pre_record_supported: bool = False
    pre_record_enabled: bool = False
    pre_record_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the websocket API."""
        return {
            "channel": self.channel,
            "name": self.name,
            "ai_types": self.ai_types,
            "streams": self.streams,
            "can_playback": self.can_playback,
            "pre_record": {
                "supported": self.pre_record_supported,
                "enabled": self.pre_record_enabled,
                "seconds": self.pre_record_seconds,
            },
        }


# What a Reolink config entry turns out to be. Only the first is in scope by default; the
# other two are the beta, and are named so the panel can say which is which rather than
# calling a doorbell an NVR.
KIND_NVR: Final = "nvr"
KIND_HUB: Final = "hub"
KIND_CAMERA: Final = "camera"


# Why a Reolink config entry was left out of the device list. Reported in diagnostics:
# an entry that is simply absent from the panel, with nothing anywhere saying why, is
# what made issue #4 unanswerable without screenshots.
EXCLUDED_DISABLED: Final = "disabled_in_home_assistant"
EXCLUDED_NOT_A_RECORDER: Final = "not_a_recorder_and_the_beta_is_off"
EXCLUDED_ON_RECORDER: Final = "already_a_channel_on_a_recorder"
EXCLUDED_UNREADABLE: Final = "kind_could_not_be_read"


@dataclass(slots=True)
class ExcludedEntry:
    """A Reolink config entry discovery left out, and why.

    Deliberately unnamed: this is what diagnostics reports, and the rule there is that no
    camera is named. The entry id is what ties a line here to the Reolink integration's
    own diagnostics.
    """

    entry_id: str
    reason: str
    kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for diagnostics."""
        return {"entry_id": self.entry_id, "reason": self.reason, "kind": self.kind}


@dataclass(slots=True)
class DeviceInfo:
    """A recording Reolink device discovered through the Reolink integration."""

    entry_id: str
    name: str
    status: str
    model: str | None = None
    sw_version: str | None = None
    connected: bool = False
    has_storage: bool = False
    reports_triggers: bool = True
    # An NVR unless the beta that includes hubs and standalone cameras is on.
    kind: str = KIND_NVR
    cameras: list[CameraInfo] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the websocket API."""
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "status": self.status,
            "model": self.model,
            "sw_version": self.sw_version,
            "connected": self.connected,
            "has_storage": self.has_storage,
            "reports_triggers": self.reports_triggers,
            "kind": self.kind,
            "cameras": [camera.as_dict() for camera in self.cameras],
        }


@dataclass(slots=True)
class Discovery:
    """What discovery found: the devices to offer, and the entries it left out."""

    devices: list[DeviceInfo] = field(default_factory=list)
    excluded: list[ExcludedEntry] = field(default_factory=list)


def _status_for_entry(entry: ConfigEntry) -> str:
    """Map a config entry state onto a status the panel can explain to the user."""
    if entry.state is ConfigEntryState.LOADED:
        return "ok"
    if entry.state is ConfigEntryState.SETUP_RETRY:
        return "not_connected"
    if entry.state is ConfigEntryState.SETUP_ERROR:
        return "setup_error"
    return "not_loaded"


@callback
def async_get_host(hass: HomeAssistant, entry_id: str) -> Any:
    """Return the live ReolinkHost for a loaded Reolink config entry.

    Raises DeviceUnavailableError if the entry is missing or not loaded, and
    ReolinkIncompatibleError if the Reolink integration no longer exposes its host
    the way we expect.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != REOLINK_DOMAIN:
        raise DeviceUnavailableError(f"No Reolink config entry '{entry_id}'")
    if entry.state is not ConfigEntryState.LOADED:
        raise DeviceUnavailableError(f"Reolink entry '{entry_id}' is not loaded")

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        raise ReolinkIncompatibleError("Reolink config entry exposes no runtime_data")
    host = getattr(runtime_data, "host", None)
    if host is None:
        raise ReolinkIncompatibleError("Reolink runtime_data exposes no host")
    api = getattr(host, "api", None)
    if api is None:
        raise ReolinkIncompatibleError("Reolink host exposes no api")

    missing = [attr for attr in _REQUIRED_API_ATTRS if not hasattr(api, attr)]
    if missing:
        raise ReolinkIncompatibleError(
            f"Reolink API is missing expected attributes: {', '.join(missing)}"
        )
    return host


@callback
def async_has_configured_nvr(hass: HomeAssistant) -> bool:
    """Return True only if the Reolink integration holds a working NVR right now.

    Deliberately about NVRs rather than devices, whatever the beta says: this gates whether
    the panel is *offered* to someone who has not set it up, and a hub or a camera is not
    yet a reason to suggest it. Strict for the same reason — an entry we could not read
    might be any of the three, and guessing wrong offers the panel to someone it cannot
    serve.
    """
    return any(device.status == "ok" for device in async_discover_devices(hass))


@callback
def async_is_compatible(hass: HomeAssistant) -> bool:
    """Return True if at least one loaded Reolink entry can be read.

    Returns True when there are no loaded entries at all: nothing is broken, the user
    simply has not set up an NVR yet.
    """
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(REOLINK_DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        return True
    for entry in loaded:
        try:
            async_get_host(hass, entry.entry_id)
        except ReolinkIncompatibleError:
            continue
        except DeviceUnavailableError:
            continue
        else:
            return True
    return False


@dataclass(slots=True)
class _ChannelDevice:
    """What Home Assistant knows about the device behind one channel."""

    name: str | None = None
    disabled: bool = False


@callback
def _async_channel_devices(
    hass: HomeAssistant, entry_id: str, api: Any
) -> dict[int, _ChannelDevice]:
    """Map channel -> the Home Assistant device for that channel, via Reolink's entities.

    Reolink addresses cameras two different ways on the same device, and both must be
    handled or cameras get the wrong name:

        NVRUID0000000001_ch1_sub                  -> channel 1
        NVRUID0000000001_CAMUID000000001_main     -> camera UID, resolved via
                                                     api.channel_for_uid()

    Getting the second form wrong is not cosmetic: the channel then falls back to
    `api.camera_name()`, which returns the *recorder's* name for channel 0 — so the
    camera list offers "Main - NVR" as though it were a camera.

    The disabled flag matters as much as the name. Disabling a camera's device under a
    recorder is a deliberate "I do not watch this camera through this recorder", and it is
    honoured in both directions: the channel is not offered, and it no longer counts as a
    reason to hide that same camera's own directly-connected entry.

    Best effort: any failure leaves the caller falling back to the device's own names.
    """
    found: dict[int, _ChannelDevice] = {}
    # Not present in every reolink_aio version; without it, UID-addressed cameras
    # simply keep the name the device reports.
    channel_for_uid = getattr(api, "channel_for_uid", None)
    try:
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
            # Camera entities only: other platforms include device-level entities, which
            # would otherwise name the recorder as though it were a camera.
            if entity.domain != "camera" or entity.device_id is None:
                continue
            parts = entity.unique_id.split("_")
            if len(parts) < 2:
                continue

            token = parts[1]
            channel: int | None = None
            if token.isdigit():
                channel = int(token)
            elif token.startswith("ch") and token[2:].isdigit():
                channel = int(token[2:])
            elif channel_for_uid is not None:
                try:
                    channel = channel_for_uid(token)
                except Exception:
                    channel = None
            if channel is None or channel < 0 or channel in found:
                continue
            device = dev_reg.async_get(entity.device_id)
            if device is None:
                continue
            found[channel] = _ChannelDevice(
                name=device.name_by_user or device.name,
                disabled=device.disabled_by is not None,
            )
    except Exception:
        _LOGGER.debug("Could not resolve camera devices from the registries", exc_info=True)
    return found


@callback
def _async_build_camera(host: Any, channel: int, name: str) -> CameraInfo:
    """Collect what we can about one channel, defensively."""
    api = host.api

    streams = [STREAM_SUB, STREAM_MAIN]
    try:
        if api.supported(channel, "autotrack_stream"):
            streams += ["autotrack_sub", "autotrack_main"]
    except Exception:
        _LOGGER.debug("Could not probe autotrack streams on channel %s", channel)

    try:
        ai_types = sorted(api.ai_supported_types(channel))
    except Exception:
        ai_types = []

    try:
        can_playback = bool(api.supported(channel, "replay"))
    except Exception:
        can_playback = True

    camera = CameraInfo(
        channel=channel,
        name=name,
        ai_types=ai_types,
        streams=streams,
        can_playback=can_playback,
    )

    # Exact pre-record time, where the camera reports it (typically battery models).
    baichuan = getattr(api, "baichuan", None)
    if baichuan is not None:
        try:
            if api.supported(channel, "pre_record"):
                camera.pre_record_supported = True
                camera.pre_record_enabled = bool(baichuan.pre_record_enabled(channel))
                seconds = baichuan.pre_record_time(channel)
                camera.pre_record_seconds = int(seconds) if seconds is not None else None
        except Exception:
            _LOGGER.debug("Could not read pre-record config on channel %s", channel)

    return camera


@callback
def _async_device_kind(api: Any) -> str:
    """Say what a readable Reolink entry actually is."""
    if not api.is_nvr:
        return KIND_CAMERA
    return KIND_HUB if api.is_hub else KIND_NVR


@callback
def _async_channel_uids(hass: HomeAssistant) -> set[str]:
    """Return the UID of every camera attached to a recorder or a hub.

    This is what keeps a camera from being listed twice when the beta includes standalone
    devices: a camera on an NVR is very often *also* set up on its own in the Reolink
    integration, and its recordings would then appear under both. The UID is Reolink's own
    identity for a camera and is what the Reolink integration keys its entities on, so it
    is the one thing that matches across the two.

    A channel whose device is disabled in Home Assistant is left out. Someone running a
    doorbell on both its recorder and its own direct connection usually disables the copy
    they do not use — and counting that copy would hide the one they kept, which is the
    wrong way round and is what issue #4 would have hit next.
    """
    uids: set[str] = set()
    for entry in hass.config_entries.async_entries(REOLINK_DOMAIN):
        try:
            api = async_get_host(hass, entry.entry_id).api
        except (DeviceUnavailableError, ReolinkIncompatibleError):
            continue
        try:
            if not api.is_nvr:
                continue
            channels = list(api.channels)
        except Exception:
            continue
        # Not in every reolink_aio version, and cosmetic to the NVR list, so a library
        # without it costs deduplication rather than the whole beta.
        camera_uid = getattr(api, "camera_uid", None)
        if camera_uid is None:
            continue
        known = _async_channel_devices(hass, entry.entry_id, api)
        for channel in channels:
            device = known.get(channel)
            if device is not None and device.disabled:
                continue
            try:
                uid = camera_uid(channel)
            except Exception:
                continue
            if uid and uid.lower() not in {"unknown", ""}:
                uids.add(uid)
    return uids


@callback
def _async_is_attached(api: Any, attached_uids: set[str]) -> bool:
    """Whether this standalone camera is already a channel on a recorder or hub."""
    if not attached_uids:
        return False
    candidates: list[str] = []
    for read in (lambda: api.uid, lambda: api.camera_uid(0)):
        try:
            value = read()
        except Exception:
            continue
        if value:
            candidates.append(value)
    return any(uid in attached_uids for uid in candidates)


@callback
def async_discover_devices(
    hass: HomeAssistant, *, include_all_devices: bool = False
) -> list[DeviceInfo]:
    """Return every recording Reolink device known to the Reolink integration."""
    return async_discover(hass, include_all_devices=include_all_devices).devices


@callback
def async_discover(hass: HomeAssistant, *, include_all_devices: bool = False) -> Discovery:
    """Return the Reolink devices to offer, and the entries left out with the reason.

    Entries that are not usable are still returned as devices, with a status explaining
    why, so the panel can tell the user *why* a device is missing instead of hiding it.
    Entries left out entirely are returned separately, for diagnostics: they are invisible
    in the panel by design, and issue #4 showed that makes them impossible to reason about
    from a bug report.

    `include_all_devices` is the beta: hubs and standalone cameras record to their own
    storage and answer the same search API, so they are worth offering to someone willing
    to report on whether it works. A camera that is already a channel on an NVR is left out
    even then — it would be the same footage listed twice, under two names.
    """
    results: list[DeviceInfo] = []
    excluded: list[ExcludedEntry] = []
    attached_uids = _async_channel_uids(hass) if include_all_devices else set()

    for entry in hass.config_entries.async_entries(REOLINK_DOMAIN):
        # Disabled in Home Assistant on purpose, so not a device that has gone missing.
        # Carding these as unavailable is what made issue #4 unreadable: eight disabled
        # camera entries showed as tiles because the status check runs before the
        # recorders-only filter, while the one working camera was filtered out — which
        # reads as "standalone cameras are supported and mine is broken" when in fact the
        # beta was simply off.
        if entry.disabled_by is not None:
            excluded.append(ExcludedEntry(entry.entry_id, EXCLUDED_DISABLED))
            continue

        status = _status_for_entry(entry)
        fallback_name = entry.title or "Reolink device"

        if status != "ok":
            # Not loaded: we cannot know whether it is an NVR, so report it and let
            # the panel show it as unavailable.
            results.append(DeviceInfo(entry_id=entry.entry_id, name=fallback_name, status=status))
            continue

        try:
            host = async_get_host(hass, entry.entry_id)
        except ReolinkIncompatibleError:
            _LOGGER.warning(
                "Reolink entry %s could not be read; the installed Reolink "
                "integration may be incompatible",
                entry.entry_id,
            )
            results.append(
                DeviceInfo(entry_id=entry.entry_id, name=fallback_name, status="incompatible")
            )
            continue
        except DeviceUnavailableError:
            results.append(
                DeviceInfo(entry_id=entry.entry_id, name=fallback_name, status="not_connected")
            )
            continue

        api = host.api

        # NVRs only, unless the beta says otherwise: hubs and standalone cameras record
        # differently, and how differently is exactly what the beta is for finding out.
        try:
            kind = _async_device_kind(api)
        except Exception:
            _LOGGER.debug("Could not tell what Reolink entry %s is", entry.entry_id, exc_info=True)
            excluded.append(ExcludedEntry(entry.entry_id, EXCLUDED_UNREADABLE))
            continue
        if kind != KIND_NVR and not include_all_devices:
            _LOGGER.debug(
                "Skipping %s: it is a %s, and the every-device beta is off",
                entry.entry_id,
                kind,
            )
            excluded.append(ExcludedEntry(entry.entry_id, EXCLUDED_NOT_A_RECORDER, kind))
            continue

        # A camera that is already a channel on a recorder is that recorder's to list.
        if kind == KIND_CAMERA and _async_is_attached(api, attached_uids):
            _LOGGER.debug("Skipping %s: this camera is already a channel on an NVR", entry.entry_id)
            excluded.append(ExcludedEntry(entry.entry_id, EXCLUDED_ON_RECORDER, kind))
            continue

        try:
            has_storage = bool(api.hdd_info)
        except Exception:
            has_storage = False

        channel_devices = _async_channel_devices(hass, entry.entry_id, api)

        cameras: list[CameraInfo] = []
        try:
            channels = list(api.stream_channels)
        except Exception:
            channels = []

        # Cosmetic only, and not present in every reolink_aio version — read it
        # defensively so an older library costs a label, not the whole device list.
        dual_lens = bool(getattr(api, "is_dual_lens", False))

        for channel in channels:
            known = channel_devices.get(channel)
            # Disabled in Home Assistant: the user has said they do not watch this camera
            # here, so offering it would be arguing with them.
            if known is not None and known.disabled:
                continue
            try:
                reported_name = api.camera_name(channel)
            except Exception:
                reported_name = f"Channel {channel}"
            name = (known.name if known else None) or reported_name or f"Channel {channel}"
            if dual_lens:
                name = f"{name} (lens {channel})"
            cameras.append(_async_build_camera(host, channel, name))

        # Per-file trigger classification needs Baichuan or parseable file names.
        # Without Baichuan we may still get triggers from file names, so this is a
        # hint for the UI rather than a hard capability.
        reports_triggers = getattr(api, "baichuan", None) is not None

        results.append(
            DeviceInfo(
                entry_id=entry.entry_id,
                name=api.nvr_name or fallback_name,
                status="ok",
                model=getattr(api, "model", None),
                sw_version=getattr(api, "sw_version", None),
                connected=True,
                has_storage=has_storage,
                reports_triggers=reports_triggers,
                kind=kind,
                cameras=cameras,
            )
        )

    results.sort(key=lambda device: device.name.casefold())
    return Discovery(devices=results, excluded=excluded)
