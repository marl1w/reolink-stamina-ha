"""Discovery of Reolink NVRs via the official Reolink integration.

This is the *only* module allowed to reach into the Reolink integration's internals.
Everything else in this integration goes through the dataclasses returned here, so that
an upstream change breaks one file with a clear error rather than the whole panel.

This is the project's one dependency on non-public API, so it is kept in a single place
and pinned by tests/test_upstream_contract.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

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


class NvrUnavailableError(HomeAssistantError):
    """Raised when a requested NVR is not currently usable."""


@dataclass(slots=True)
class CameraInfo:
    """A single camera channel on an NVR."""

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


@dataclass(slots=True)
class NvrInfo:
    """An NVR discovered through the Reolink integration."""

    entry_id: str
    name: str
    status: str
    model: str | None = None
    sw_version: str | None = None
    connected: bool = False
    has_storage: bool = False
    reports_triggers: bool = True
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
            "cameras": [camera.as_dict() for camera in self.cameras],
        }


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

    Raises NvrUnavailableError if the entry is missing or not loaded, and
    ReolinkIncompatibleError if the Reolink integration no longer exposes its host
    the way we expect.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != REOLINK_DOMAIN:
        raise NvrUnavailableError(f"No Reolink config entry '{entry_id}'")
    if entry.state is not ConfigEntryState.LOADED:
        raise NvrUnavailableError(f"Reolink entry '{entry_id}' is not loaded")

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

    Deliberately strict: an entry we could not read might be a camera, a hub or a
    recorder, and guessing wrong means offering the panel to someone it cannot serve.
    """
    return any(nvr.status == "ok" for nvr in async_discover_nvrs(hass))


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
        except NvrUnavailableError:
            continue
        else:
            return True
    return False


@callback
def _async_camera_channels(hass: HomeAssistant, entry_id: str, api: Any) -> dict[int, str]:
    """Map channel -> user-facing camera name, from the Reolink camera entities.

    Reolink addresses cameras two different ways in the same NVR, and both must be
    handled or cameras get the wrong name:

        NVRUID0000000001_ch1_sub                  -> channel 1
        NVRUID0000000001_CAMUID000000001_main     -> camera UID, resolved via
                                                     api.channel_for_uid()

    Getting the second form wrong is not cosmetic: the channel then falls back to
    `api.camera_name()`, which returns the *recorder's* name for channel 0 — so the
    camera list offers "Main - NVR" as though it were a camera.

    Best effort: any failure leaves the caller falling back to the NVR's own names.
    """
    names: dict[int, str] = {}
    # Not present in every reolink_aio version; without it, UID-addressed cameras
    # simply keep the name the NVR reports.
    channel_for_uid = getattr(api, "channel_for_uid", None)
    try:
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
            # Camera entities only: other platforms include NVR-level entities, which
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
            if channel is None or channel < 0 or channel in names:
                continue
            device = dev_reg.async_get(entity.device_id)
            if device is None:
                continue
            name = device.name_by_user or device.name
            if name:
                names[channel] = name
    except Exception:
        _LOGGER.debug("Could not resolve camera names from the registries", exc_info=True)
    return names


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
def async_discover_nvrs(hass: HomeAssistant) -> list[NvrInfo]:
    """Return every Reolink NVR known to the Reolink integration.

    Entries that are not usable are still returned, with a status explaining why, so
    the panel can tell the user *why* an NVR is missing instead of hiding it.
    """
    results: list[NvrInfo] = []

    for entry in hass.config_entries.async_entries(REOLINK_DOMAIN):
        status = _status_for_entry(entry)
        fallback_name = entry.title or "Reolink device"

        if status != "ok":
            # Not loaded: we cannot know whether it is an NVR, so report it and let
            # the panel show it as unavailable.
            results.append(NvrInfo(entry_id=entry.entry_id, name=fallback_name, status=status))
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
                NvrInfo(entry_id=entry.entry_id, name=fallback_name, status="incompatible")
            )
            continue
        except NvrUnavailableError:
            results.append(
                NvrInfo(entry_id=entry.entry_id, name=fallback_name, status="not_connected")
            )
            continue

        api = host.api

        # NVRs only. Standalone cameras and hubs record differently and are out of scope.
        try:
            if not api.is_nvr or api.is_hub:
                continue
        except Exception:
            continue

        try:
            has_storage = bool(api.hdd_info)
        except Exception:
            has_storage = False

        camera_channels = _async_camera_channels(hass, entry.entry_id, api)

        cameras: list[CameraInfo] = []
        try:
            channels = list(api.stream_channels)
        except Exception:
            channels = []

        # Cosmetic only, and not present in every reolink_aio version — read it
        # defensively so an older library costs a label, not the whole NVR list.
        dual_lens = bool(getattr(api, "is_dual_lens", False))

        for channel in channels:
            try:
                nvr_name = api.camera_name(channel)
            except Exception:
                nvr_name = f"Channel {channel}"
            name = camera_channels.get(channel) or nvr_name or f"Channel {channel}"
            if dual_lens:
                name = f"{name} (lens {channel})"
            cameras.append(_async_build_camera(host, channel, name))

        # Per-file trigger classification needs Baichuan or parseable file names.
        # Without Baichuan we may still get triggers from file names, so this is a
        # hint for the UI rather than a hard capability.
        reports_triggers = getattr(api, "baichuan", None) is not None

        results.append(
            NvrInfo(
                entry_id=entry.entry_id,
                name=api.nvr_name or fallback_name,
                status="ok",
                model=getattr(api, "model", None),
                sw_version=getattr(api, "sw_version", None),
                connected=True,
                has_storage=has_storage,
                reports_triggers=reports_triggers,
                cameras=cameras,
            )
        )

    results.sort(key=lambda nvr: nvr.name.casefold())
    return results
