"""Pass the recorder's playback stream through to the browser.

The recorder answers `cmd=Playback` with FLV — a container, not something that needs
re-encoding. The browser can demux it itself through Media Source Extensions, which is
exactly what the recorder's own web player does, so all that is needed here is a pipe.

That matters more than it sounds. The previous design fed the same stream to Home
Assistant's stream component, which remuxed it to HLS with ffmpeg. Every clip opened
became a subprocess pulling video in real time, and abandoned ones accumulated until the
machine stopped responding. Piping bytes has no such failure mode: when the browser stops
reading, the connection closes and nothing is left behind.

Nothing is buffered to disk, and nothing is held in memory beyond one chunk.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from aiohttp import ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import STREAM_MAIN, STREAM_SUB
from .reolink_registry import DeviceUnavailableError, ReolinkIncompatibleError, async_get_host

_LOGGER = logging.getLogger(__name__)

# The recorder's own stream selector: 1 is the sub stream, 0 the main one.
PLAYBACK_STREAM_TYPE = {STREAM_SUB: 1, STREAM_MAIN: 0}

# Generous: the recorder sends at roughly real time, so a long clip takes a long time.
# The browser closing the connection is what normally ends it.
STREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=15, sock_read=60)

_CHUNK = 65536


def async_flv_path(
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start_id: str,
    playback_id: str,
    seek: int,
) -> str:
    """Return the unsigned path for streaming one recording."""
    from base64 import urlsafe_b64encode

    encoded = urlsafe_b64encode(filename.encode()).decode()
    return (
        f"/api/reolink_stamina/flv/{entry_id}/{channel}/{stream}"
        f"/{encoded}/{start_id}/{playback_id}/{max(0, int(seek))}"
    )


async def async_playback_source(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start_id: str,
    playback_id: str,
    seek: int,
) -> str:
    """Build the recorder's playback URL, as its own web player builds it.

    Every parameter matters. `start` is StartTime while `playbackTime` is the same instant
    in UTC, and both are required; `type` selects the resolution numerically; `channel` and
    `seek` are mandatory even at offset zero. Omitting any one of them makes the recorder
    answer 404 or drop the connection.

    reolink_aio's own playback URL omits four of them and derives `start` by
    pattern-matching the file name, which never matches the synthetic names a recorder
    returns, so a library-built URL cannot be used here.
    """
    from reolink_aio.enums import VodRequestType

    api = async_get_host(hass, entry_id).api

    # Borrow a library-built URL for its base address and freshly minted token, so
    # authentication and renewal stay the library's problem.
    _mime, template = await api.get_vod_source(channel, filename, stream, VodRequestType.PLAYBACK)
    parts = urlsplit(template)
    token = parse_qs(parts.query).get("token", [""])[0]

    query: dict[str, Any] = {
        "cmd": "Playback",
        "channel": channel,
        "type": PLAYBACK_STREAM_TYPE.get(stream, 1),
        "start": start_id,
        "seek": max(0, int(seek)),
        "source": filename,
        "playbackTime": playback_id,
    }
    if token:
        query["token"] = token

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class ReolinkStaminaFlvView(HomeAssistantView):
    """Stream a recording's FLV straight through to the browser."""

    # Everything is in the path rather than the query string, so the whole URL can be
    # signed by the panel without ambiguity about what was covered by the signature.
    url = (
        "/api/reolink_stamina/flv/{entry_id}/{channel}/{stream}"
        "/{filename}/{start_id}/{playback_id}/{seek}"
    )
    name = "api:reolink_stamina:flv"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        channel: str,
        stream: str,
        filename: str,
        start_id: str,
        playback_id: str,
        seek: str,
    ) -> Any:
        """Pipe the recording to the client."""
        hass: HomeAssistant = request.app["hass"]

        try:
            name = urlsafe_b64decode(filename.encode()).decode()
            channel_no = int(channel)
            seek_seconds = max(0, int(seek))
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="Malformed recording reference")

        try:
            source = await async_playback_source(
                hass,
                entry_id,
                channel_no,
                stream,
                name,
                start_id,
                playback_id,
                seek_seconds,
            )
        except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
            return web.Response(status=404, text=str(err))
        except Exception as err:
            _LOGGER.debug("Could not resolve a playback URL", exc_info=True)
            return web.Response(status=502, text=f"Could not open the recording: {err}")

        session = async_get_clientsession(hass)
        try:
            upstream = await session.get(source, timeout=STREAM_TIMEOUT)
        except Exception as err:
            return web.Response(status=502, text=f"The device did not answer: {err}")

        if upstream.status != 200:
            upstream.release()
            return web.Response(status=502, text=f"The device answered HTTP {upstream.status}")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": upstream.headers.get("Content-Type", "video/x-flv"),
                # Live-paced and unseekable by byte, so nothing should try to cache or
                # range-request it.
                "Cache-Control": "no-store",
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)

        try:
            async for chunk in upstream.content.iter_chunked(_CHUNK):
                await response.write(chunk)
        except (ConnectionResetError, ConnectionError, TimeoutError):
            # The browser navigated away, seeked, or opened another clip. Normal.
            _LOGGER.debug("Playback client disconnected")
        except Exception:
            _LOGGER.debug("Playback stream ended unexpectedly", exc_info=True)
        finally:
            # Closing the upstream response is what stops the recorder sending, so it must
            # happen however the client went away.
            upstream.close()

        return response
