"""Finding the ffmpeg Home Assistant is configured with.

Two features need it and neither is on by default: cutting a clip out of 24/7 footage for
cloud sync, and the beta adaptive playback route. Everything else in this integration
works on an installation with no ffmpeg at all, which is why this is asked for per
request rather than checked at setup.
"""

from __future__ import annotations

import shutil

from homeassistant.core import HomeAssistant


def async_ffmpeg_binary(hass: HomeAssistant) -> str | None:
    """Return the ffmpeg to use, preferring the one Home Assistant is configured with.

    Falls back to whatever is on PATH, so a container that ships ffmpeg without the
    Home Assistant ffmpeg integration set up still works.
    """
    try:
        from homeassistant.components.ffmpeg import (
            get_ffmpeg_manager,
        )

        manager = get_ffmpeg_manager(hass)
    except (ImportError, KeyError, AttributeError):
        manager = None
    if manager is not None and getattr(manager, "binary", None):
        return str(manager.binary)
    return shutil.which("ffmpeg")
