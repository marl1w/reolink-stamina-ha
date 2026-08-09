"""Which recorders and cameras there are to browse.

Cheap: everything comes from the already-running Reolink integration, with no call to a
recorder. It is also where the panel learns its own options, so one round trip tells it what
to draw and how to behave.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
import voluptuous as vol

from ..const import DOMAIN, SEARCH_WINDOW_DAYS
from ..reolink_registry import async_discover_devices
from .shared import _access

_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/devices"})
@callback
def ws_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the recording devices discovered through the Reolink integration.

    Cheap: everything comes from the already-running Reolink integration, with no call
    to the recorder itself.
    """
    data = _access(hass, connection, msg)
    if data is None:
        return

    options = data.options
    try:
        devices = [
            device.as_dict() for device in async_discover_devices(hass, include_all_devices=True)
        ]
    except Exception as err:
        # Without this the panel shows a bare "Unknown error" and the reason is only in
        # the log. Discovery reads a non-public Reolink attribute, so say what broke.
        _LOGGER.exception("Reolink device discovery failed")
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNKNOWN_ERROR,
            f"Could not read the Reolink integration: {err}",
        )
        return

    connection.send_result(
        msg["id"],
        {
            "devices": devices,
            "options": options.as_dict(),
            "search_window_days": SEARCH_WINDOW_DAYS,
        },
    )
