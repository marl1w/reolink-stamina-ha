"""What every websocket command needs before it can answer.

The access check is the important one and it answers two questions at once: is the
integration still loaded, and may this user see recordings. An open panel outlives an unload
or a reload, so both have to be asked on every command rather than once at registration.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from ..const import DOMAIN, SEARCH_WINDOW_DAYS, STREAM_MAIN, STREAM_SUB

_LOGGER = logging.getLogger(__name__)

# A generous ceiling that still prevents one subscription asking a device for thousands
# of searches.
MAX_BUCKETS = 240

# What the panel may ask `stream_url` for. Passthrough is the recorder's own bytes, sent on
# untouched; the other two are conversions.
ROUTE_PASSTHROUGH = "passthrough"
ROUTE_REMUX = "remux"
ROUTE_TRANSCODE = "transcode"

TARGET_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("channel"): vol.Coerce(int),
    }
)


@callback
def _runtime(hass: HomeAssistant) -> Any:
    """Return this integration's runtime data, or None if it is not set up."""
    return hass.data.get(DOMAIN)


@callback
def _access(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> Any | None:
    """Return the runtime data if this connection may use the panel, else None.

    Answers both "is the integration still loaded" and "is this user allowed", because
    an open panel can outlive an unload or a reload. The admin check mirrors the panel's
    own option rather than hard-coding admin-only, so the two cannot disagree.
    """
    data = _runtime(hass)
    if data is None:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            "Reolink Stamina is not set up",
        )
        return None

    if data.options.require_admin and not connection.user.is_admin:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNAUTHORIZED,
            "Only administrators may browse Reolink recordings",
        )
        return None

    return data


def _parse_date(value: str) -> dt.date:
    """Parse an ISO date, raising a websocket-friendly error."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as err:
        raise vol.Invalid(f"Invalid date '{value}'") from err


def _clamp_range(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """Clamp a requested range to what the device can actually search."""
    today = dt_util.now().date()
    earliest = today - dt.timedelta(days=SEARCH_WINDOW_DAYS)
    start = max(start, earliest)
    end = min(end, today)
    if end < start:
        end = start
    return start, end


def _dates_in(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every date in an inclusive range, newest first."""
    span = (end - start).days
    return [end - dt.timedelta(days=offset) for offset in range(span + 1)]


@callback
def _secondary_stream(primary: str) -> str:
    """Return the other resolution, used only to work out clip availability."""
    if primary == STREAM_SUB:
        return STREAM_MAIN
    if primary == STREAM_MAIN:
        return STREAM_SUB
    # Autotrack streams have no meaningful counterpart; compare against low res.
    return STREAM_SUB
