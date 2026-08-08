"""Diagnostics for Reolink Stamina.

Written for one problem in particular: adaptive playback failing on other people's machines
and nowhere else. A 502 from a conversion reaches the browser as a numeric `MediaError`, the
person reporting it cannot be asked to run shell commands, and the maintainer cannot
reproduce it — so the evidence has to be collectable in one click from the config entry.

What is here is chosen to answer the questions that actually come up:

* *Why did it fail?* — the recent conversion failures, each already classified into a cause
  by `restream.py` rather than left as raw ffmpeg output.
* *Is the hardware encoder the problem?* — which one was chosen, and which have been
  disabled after failing.
* *Has the machine run out of somewhere to write?* — free space where segments go, and how
  many session directories were left behind. A count that climbs with uptime is a leak, and
  a full temporary filesystem makes every conversion fail at once.
* *Do the clocks agree?* — Home Assistant's timezone and each recorder's, plus the
  timestamps a cached recording was described by. Playback is addressed by timestamp, so a
  recorder keeping a different time from Home Assistant asks for a moment it has nothing
  recorded at and answers 404 — a clip that never plays, with nothing in the log tying it
  to a clock.

No credential appears here, and no camera is named. Recording file names do, in the playback
samples and nowhere else: whether a recorder names its recordings or returns a bare timestamp
decides how a playback URL has to address them, so it is part of the evidence.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import shutil
import tempfile
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .ffmpeg import async_ffmpeg_binary
from .playback_route import async_all_routes
from .reolink_registry import async_discover_devices, async_get_host
from .restream import SESSION_PREFIX, async_beta_enabled, async_get_manager


def _clock(hass: HomeAssistant) -> dict[str, Any]:
    """Return Home Assistant's clock and each recorder's, so the two can be compared.

    Playback is addressed by timestamp: the recorder's `PlaybackTime` is converted into its
    own local time to say which recording is wanted. Both halves of that have to be right,
    and only one of them is measured — which zone `PlaybackTime` arrives in is, but a
    recorder whose clock simply disagrees with Home Assistant's is not something a search
    result can reveal. Either way the converted timestamp names a moment the recorder has no
    recording for and it answers 404 — which reads as a clip that will not play, with
    nothing in the log to connect it to a clock.

    A report that shows the two offsets side by side turns that into a one-line diagnosis.
    """
    now = dt_util.utcnow()
    local = dt_util.as_local(now)
    clock: dict[str, Any] = {
        "home_assistant_timezone": hass.config.time_zone,
        "home_assistant_utc_offset": local.strftime("%z"),
        "home_assistant_utc": now.isoformat(),
        "recorders": [],
    }

    for device in async_discover_devices(hass, include_all_devices=True):
        recorder: dict[str, Any] = {"name": device.name, "model": device.model}
        try:
            api = async_get_host(hass, device.entry_id).api
            tzinfo = api.timezone()
            device_time = api.time()
            # Named for the moment being reported, not for the zone in the abstract. A
            # recorder's tzinfo prints as its *standard* offset when asked without one --
            # "UTC-05:00" for a device sitting on -04:00 in summer -- which reads as a
            # missing DST adjustment and sent the first report of issue #1 after one.
            zone = tzinfo.tzname(device_time) if tzinfo is not None else None
            recorder["timezone"] = zone or "not reported"
            recorder["time"] = (
                device_time.isoformat() if device_time is not None else "not reported"
            )
            if device_time is not None:
                # The number that matters: a recorder whose clock disagrees with Home
                # Assistant's by a whole number of hours is the whole bug.
                recorder["drift_from_ha_seconds"] = round(
                    (device_time - dt_util.as_local(now)).total_seconds()
                )
        except Exception as err:
            recorder["error"] = f"{type(err).__name__}: {err}"
        clock["recorders"].append(recorder)

    return clock


def _temporary_space() -> dict[str, Any]:
    """Return free space where HLS sessions are written, and what earlier ones left behind.

    The leftover count is the interesting number. Each session writes into its own directory
    and removes it on the way out, and anything older than that is swept at setup — so on a
    healthy installation this is the one or two sessions currently playing, and nothing else.
    A number that climbs the longer Home Assistant has been up is a live leak, and one that
    keeps climbing until conversions start failing is where the space went.
    """
    root = Path(tempfile.gettempdir())
    leftovers: list[Path] = []
    with contextlib.suppress(OSError):
        leftovers = [path for path in root.glob(f"{SESSION_PREFIX}*") if path.is_dir()]

    total = 0
    for directory in leftovers:
        try:
            total += sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
        except OSError:
            continue

    space: dict[str, Any] = {
        "path": str(root),
        "session_directories": len(leftovers),
        "session_bytes": total,
    }
    try:
        usage = shutil.disk_usage(root)
        space["free_bytes"] = usage.free
        space["total_bytes"] = usage.total
    except OSError:
        pass
    return space


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return everything worth having in a bug report about playback."""
    data = hass.data.get(DOMAIN)
    manager = async_get_manager(hass)
    binary = async_ffmpeg_binary(hass)

    return {
        "options": data.options.as_dict() if data is not None else None,
        "adaptive_playback": {
            "enabled": async_beta_enabled(hass),
            "ffmpeg": binary or "not found",
            # Newest last, and already reduced to a cause rather than raw ffmpeg output.
            "failures": list(manager.failures),
            "encoder": manager.encoder.name if manager.encoder is not None else "not yet probed",
            # A name here means that encoder failed in the field and is no longer chosen.
            "disabled_encoders": sorted(manager.failed_encoders),
        },
        "clocks": _clock(hass),
        # Which endpoint each recorder was measured to serve playback from, keyed by
        # Reolink config entry. Recorders disagree about this and the disagreement does not
        # follow the model, so "which route is this device on" is the first question a
        # report about a clip that will not play has to answer. Empty until a recording has
        # been opened this run -- the measurement is not made until it is needed.
        "playback_routes": async_all_routes(hass),
        # The timestamps one recording was described by, which is what a playback URL is
        # built from. `playback_id` is the recording's own start as the recorder stated it,
        # `file_start_id` is that same instant in the recorder's local time and is what the
        # recorder is asked for, and `start_id` is the window being shown. A `file_start_id`
        # the recorder has nothing at is how a 404 happens, and these three side by side are
        # what show it. `playback_is_utc` is which zone `playback_id` was taken to be in,
        # measured per search -- so a wrong `file_start_id` says whether the measurement or
        # the arithmetic was at fault.
        "playback_samples": data.cache.sample_files() if data is not None else [],
        "temporary_space": await hass.async_add_executor_job(_temporary_space),
    }
