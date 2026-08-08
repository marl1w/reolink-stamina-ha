"""Handing the browser a URL for a clip it can save.

Separate from playback: what plays in a panel and what somebody keeps are different files,
cut to different bounds, and conflating them is how a download came to hold five minutes of
an empty drive.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from ..const import DOMAIN
from ..fragments import FragmentsUnsupportedError, async_fragment_path
from .shared import _access

_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/clip_url",
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
        vol.Required("stream"): cv.string,
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
    }
)
@callback
def ws_clip_url(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a path that serves exactly this stretch of footage as an MP4.

    The recorder cuts it — see fragments.py. Unsigned, like the playback path: the panel
    signs it with Home Assistant's own command so a plain download link can fetch it.
    """
    if _access(hass, connection, msg) is None:
        return

    try:
        start = dt.datetime.fromisoformat(msg["start"])
        end = dt.datetime.fromisoformat(msg["end"])
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    try:
        path = async_fragment_path(msg["entry_id"], msg["channel"], msg["stream"], start, end)
    except FragmentsUnsupportedError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_NOT_SUPPORTED, str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_INVALID_FORMAT, str(err))
        return

    connection.send_result(msg["id"], {"path": path, "mime": "video/mp4"})
