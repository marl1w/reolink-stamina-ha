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

Nothing here names a camera, a recording or a credential.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import shutil
import tempfile
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ffmpeg import async_ffmpeg_binary
from .restream import SESSION_PREFIX, async_beta_enabled, async_get_manager


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
        "temporary_space": await hass.async_add_executor_job(_temporary_space),
    }
