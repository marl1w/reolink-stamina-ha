"""Clips cut by the recorder itself.

The NVR has a command — `NvrDownload` — that takes a start and an end time and prepares
an MP4 covering exactly that stretch, which it then serves from its ordinary download
endpoint. That is worth a great deal:

* No trimming. Playback can only be *started* at an offset, so cutting a clip out of a
  half-hour recording previously meant reading the stream and stopping at the right
  moment, then rebuilding a container around the samples. The recorder does all of it.
* No pacing. The playback endpoint streams at roughly the speed the footage was filmed;
  a prepared file comes down as fast as the network allows.
* MP4, not FLV, without a muxer on either side.

**Not usable on every recorder, and not on the ones tested.** An RLN8-410 on firmware
v3.6.5.562 accepts the command and reports a prepared fragment with a plausible size, and
then drops the connection on every attempt to download it — with or without `&start=`, with
or without `&output=`, and after waiting up to twenty seconds for it to settle. Reolink's own
media source never selects this route for an NVR whose recordings are named `.mp4`, which on
this evidence looks deliberate rather than incidental.

So the panel does not use this for its clip downloads; it builds the MP4 in the browser from
the playback stream instead. This module stays because the command is the right shape for
the job and may work on a Home Hub or a later firmware — but anything switching to it needs
to prove the *download* works on real hardware, not just the request.

Preparing a fragment also costs the recorder real work, so this is not something to call
speculatively.

Home Assistant's own Reolink integration already proxies its recordings through an
authenticated view, and that is what is used here rather than a second connection to the
device: it holds one session per NVR, and adding more is how you get a recorder that
refuses to talk to anyone. The coupling to that view lives in this module alone, and
tests/test_upstream_contract.py pins it.
"""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

# A ceiling on what may be asked for in one fragment. The recorder has to assemble the
# whole thing before it sends a byte, and a request for an afternoon would simply hang.
MAX_FRAGMENT_SECONDS = 15 * 60

# What reolink_aio calls this request type. Its own enum member is `NVR_DOWNLOAD`, whose
# *value* is this string, and the value is what travels in the proxy URL.
VOD_TYPE_NVR_DOWNLOAD = "NvrDownload"


class FragmentsUnsupportedError(Exception):
    """Raised when this Home Assistant cannot proxy a recorder-cut fragment."""


def reolink_time_id(moment: dt.datetime) -> str:
    """Format an instant the way the recorder's own API expects it.

    Naive local time, as everywhere else the device is addressed: reolink_aio reads the
    fields off the datetime without converting, so an aware timestamp in another zone
    would silently ask for the wrong footage.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return (
        f"{moment.year}{moment.month:02}{moment.day:02}"
        f"{moment.hour:02}{moment.minute:02}{moment.second:02}"
    )


@callback
def async_fragment_path(
    entry_id: str,
    channel: int,
    stream: str,
    start: dt.datetime,
    end: dt.datetime,
) -> str:
    """Return the unsigned path that serves a recorder-cut MP4 for one time range.

    Raises FragmentsUnsupportedError if the Reolink integration no longer exposes the
    proxy this relies on, so the caller can fall back rather than fail.
    """
    try:
        from homeassistant.components.reolink.views import (
            async_generate_playback_proxy_url,
        )
    except ImportError as err:  # pragma: no cover - exercised by the contract test
        raise FragmentsUnsupportedError(
            "The installed Reolink integration does not expose its playback proxy"
        ) from err

    if end <= start:
        raise ValueError("A fragment must end after it starts")
    length = (end - start).total_seconds()
    if length > MAX_FRAGMENT_SECONDS:
        raise ValueError(
            f"A fragment may cover at most {MAX_FRAGMENT_SECONDS}s, asked for {length:.0f}s"
        )

    # The two times travel joined by an underscore in place of a file name; reolink_aio
    # splits them back apart and turns them into the NvrDownload request.
    name = f"{reolink_time_id(start)}_{reolink_time_id(end)}"
    _LOGGER.debug(
        "Asking %s channel %s for a %.0fs %s fragment at %s",
        entry_id,
        channel,
        length,
        stream,
        name,
    )
    return async_generate_playback_proxy_url(entry_id, channel, name, stream, VOD_TYPE_NVR_DOWNLOAD)
