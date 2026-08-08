"""The panel's websocket API, one module per thing it can be asked.

Split out of a single file that had grown to a thousand lines holding six unrelated
conversations: browsing recordings, getting one to play, saving one, and three questions
about the relevance beta. They share only the access check, which is in `shared`.

Registration stays here so there is exactly one list of what exists, and adding a command
means touching one line in it rather than remembering that such a list is somewhere.
"""

from __future__ import annotations

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .browse import ws_calendar, ws_events
from .devices import ws_devices
from .download import ws_clip_url
from .playback import ws_playback_failure, ws_stream_url
from .relevance import ws_detections, ws_relevance, ws_relevance_profile

__all__ = ["async_register"]


def async_register(hass: HomeAssistant) -> None:
    """Register every websocket command."""
    for command in (
        ws_devices,
        ws_events,
        ws_calendar,
        ws_stream_url,
        ws_detections,
        ws_relevance,
        ws_relevance_profile,
        ws_clip_url,
        ws_playback_failure,
    ):
        websocket_api.async_register_command(hass, command)
