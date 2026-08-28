"""Pass the recorder's playback stream through to the browser.

The recorder answers playback requests with FLV — a container, not something that needs
re-encoding. The browser can demux it itself through Media Source Extensions, which is
exactly what the recorder's own web player does, so all that is needed here is a pipe.

That matters more than it sounds. The previous design fed the same stream to Home
Assistant's stream component, which remuxed it to HLS with ffmpeg. Every clip opened
became a subprocess pulling video in real time, and abandoned ones accumulated until the
machine stopped responding. Piping bytes has no such failure mode: when the browser stops
reading, the connection closes and nothing is left behind.

Nothing is buffered to disk, and nothing is held in memory beyond one chunk.

Which endpoint on the recorder the bytes come from is `playback_route.py`'s problem, not
this module's: recorders disagree about that, and the disagreement is measured rather
than assumed.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .playback_route import (
    ROUTE_ORDER_REALTIME,
    PlaybackRouteError,
    Recording,
    async_open_playback_stream,
)
from .redact import scrub_credentials
from .reolink_registry import DeviceUnavailableError, ReolinkIncompatibleError

_LOGGER = logging.getLogger(__name__)

FLV_PREFIX = "/api/reolink_stamina/flv"

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
    return Recording(
        entry_id=entry_id,
        channel=channel,
        stream=stream,
        filename=filename,
        start_id=start_id,
        playback_id=playback_id,
        seek=max(0, int(seek)),
    ).path(FLV_PREFIX)


class ReolinkStaminaFlvView(HomeAssistantView):
    """Stream a recording's FLV straight through to the browser."""

    # Everything is in the path rather than the query string, so the whole URL can be
    # signed by the panel without ambiguity about what was covered by the signature.
    url = (
        f"{FLV_PREFIX}/{{entry_id}}/{{channel}}/{{stream}}"
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
            recording = Recording.from_path(
                entry_id, channel, stream, filename, start_id, playback_id, seek
            )
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="Malformed recording reference")

        try:
            # Only the live-paced routes: a whole file is not something this view can
            # pipe, and a recorder that has nothing else is handed to the browser as a
            # file rather than reaching here at all.
            upstream, _route, _url = await async_open_playback_stream(
                hass, recording, routes=ROUTE_ORDER_REALTIME
            )
        except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
            return web.Response(status=404, text=str(err))
        except PlaybackRouteError as err:
            # Already scrubbed where the message was built, and scrubbed again here
            # because this is the last place the text passes before a browser sees it.
            return web.Response(status=502, text=scrub_credentials(str(err)))
        except Exception as err:
            _LOGGER.debug("Could not open a playback stream", exc_info=True)
            return web.Response(
                status=502, text=f"Could not open the recording: {scrub_credentials(str(err))}"
            )

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
