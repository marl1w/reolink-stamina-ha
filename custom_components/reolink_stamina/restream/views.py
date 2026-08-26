"""The HTTP endpoints a converted recording is fetched from."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from ..playback_route import (
    PlaybackRouteError,
    Recording,
    async_playback_input_seek,
    async_playback_secrets,
    async_playback_source,
)
from ..redact import scrub_credentials
from ..reolink_registry import DeviceUnavailableError, ReolinkIncompatibleError
from .common import (
    _CHUNK,
    _HLS_FILE,
    FIRST_OUTPUT_TIMEOUT,
    FORMAT_MP4,
    HLS_INIT,
    HLS_MIN_SEGMENTS,
    HLS_PLAYLIST,
    RESTREAM_MODES,
    RESTREAM_PREFIX,
)
from .diagnosis import (
    Diagnosis,
    _cpu_load,
    _cpu_seconds,
    _diagnose_no_output,
)
from .runner import (
    FfmpegUnavailableError,
    _async_spawn,
)
from .sessions import (
    _HlsStream,
    _Stream,
    async_get_manager,
)

_LOGGER = logging.getLogger(__name__)

_MAX_DURATION = 15 * 60


class ReolinkStaminaRestreamView(HomeAssistantView):
    """Serve a recording as fragmented MP4, repackaged or re-encoded."""

    url = (
        f"{RESTREAM_PREFIX}/{{mode}}/{{entry_id}}/{{channel}}/{{stream}}"
        "/{filename}/{start_id}/{playback_id}/{seek}"
    )
    name = "api:reolink_stamina:restream"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        mode: str,
        entry_id: str,
        channel: str,
        stream: str,
        filename: str,
        start_id: str,
        playback_id: str,
        seek: str,
    ) -> Any:
        """Convert the recording on its way to the browser."""
        hass: HomeAssistant = request.app["hass"]

        if mode not in RESTREAM_MODES:
            return web.Response(status=400, text="Unknown conversion mode")

        try:
            recording = Recording.from_path(
                entry_id, channel, stream, filename, start_id, playback_id, seek
            )
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="Malformed recording reference")

        try:
            duration = float(request.query.get("duration", 0))
        except ValueError:
            return web.Response(status=400, text="Malformed duration")
        if not 0 <= duration <= _MAX_DURATION:
            return web.Response(status=400, text="Duration is out of range")

        try:
            source = await async_playback_source(hass, recording)
        except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
            return web.Response(status=404, text=str(err))
        except PlaybackRouteError as err:
            return web.Response(status=502, text=scrub_credentials(str(err)))
        except Exception as err:
            _LOGGER.debug("Could not resolve a playback URL", exc_info=True)
            return web.Response(
                status=502, text=f"Could not open the recording: {scrub_credentials(str(err))}"
            )

        credentials = async_playback_secrets(hass, entry_id)
        label = f"{recording.label} mp4"
        manager = async_get_manager(hass)

        try:
            process, encoder = await _async_spawn(
                hass,
                source,
                label=label,
                mode=mode,
                output_format=FORMAT_MP4,
                input_seek=async_playback_input_seek(hass, recording),
                duration=duration,
            )
        except FfmpegUnavailableError as err:
            return web.Response(status=501, text=str(err))
        except Exception as err:
            _LOGGER.debug("Could not start ffmpeg", exc_info=True)
            return web.Response(status=502, text=f"Could not start the conversion: {err}")

        active = _Stream(process, label, encoder, mode, credentials)
        await manager.async_claim(active)

        # Wait for the first bytes before answering, so a conversion that fails outright is
        # an error the panel can read rather than an empty video and a silent log line.
        started = time.monotonic()
        cpu_before = await hass.async_add_executor_job(_cpu_seconds, process.pid)
        try:
            first = await asyncio.wait_for(
                process.stdout.read(_CHUNK), timeout=FIRST_OUTPUT_TIMEOUT
            )
        except Exception:
            # Timed out, or the process was killed because another stream took the slot.
            first = b""

        if not first:
            elapsed = time.monotonic() - started
            cpu_after = await hass.async_add_executor_job(_cpu_seconds, process.pid)
            problem = _diagnose_no_output(
                active,
                elapsed=elapsed,
                load=_cpu_load(cpu_before, cpu_after, elapsed),
                # There is no header to look for on a pipe, so the recorder answering and
                # the recorder never answering are not separable here.
                opened=None,
                progress="no video at all",
            )
            # An evicted stream produced nothing because it was killed, which is not
            # evidence about anything and must not be recorded as though it were.
            if manager.holds(active):
                manager.note_failure(active, problem, mode=mode)
            await manager.async_release(active)
            return web.Response(status=502, text=problem.message)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp4",
                # Live-paced, of unknown length, and unseekable by byte.
                "Cache-Control": "no-store",
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)

        try:
            await response.write(first)
            while chunk := await process.stdout.read(_CHUNK):
                await response.write(chunk)
        except (ConnectionResetError, ConnectionError, TimeoutError):
            _LOGGER.debug("Restream client disconnected")
        except Exception:
            _LOGGER.debug("Restream ended unexpectedly", exc_info=True)
        finally:
            await manager.async_release(active)

        return response


class ReolinkStaminaHlsView(HomeAssistantView):
    """Serve a live HLS session's playlist and segments.

    Unauthenticated, and protected by the session token in the path instead — the same
    trade Home Assistant's own camera streams make, and for the same reason: iOS hands
    playback to the system player, which sends none of Home Assistant's authentication.
    A token is 32 random characters, names exactly one recording, and stops existing a
    minute after the last segment is read.
    """

    url = "/api/reolink_stamina/hls/{token}/{filename}"
    name = "api:reolink_stamina:hls"
    requires_auth = False

    async def get(self, request: web.Request, token: str, filename: str) -> Any:
        """Return one playlist, one init segment or one media segment."""
        hass: HomeAssistant = request.app["hass"]

        if not _HLS_FILE.match(filename):
            return web.Response(status=400, text="Not a playlist or a segment")

        manager = async_get_manager(hass)
        session = manager.hls_session(token)
        if session is None:
            # Expected: the session was replaced or timed out. The panel reopens.
            return web.Response(status=404, text="This playback session has ended")
        session.touch()

        path = session.directory / filename
        if filename == HLS_PLAYLIST:
            problem = await _async_wait_for_playlist(hass, session)
            if problem is not None:
                # A stream that was evicted mid-startup produced nothing because it was
                # killed, which says nothing about the recorder, the machine or the encoder
                # and must not be recorded as though it did.
                if manager.holds(session):
                    manager.note_failure(session, problem, mode=session.mode)
                await manager.async_release(session)
                return web.Response(status=502, text=problem.message)
            body = await hass.async_add_executor_job(path.read_bytes)
            return web.Response(
                body=body,
                content_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )

        if not await hass.async_add_executor_job(path.is_file):
            return web.Response(status=404, text="No such segment")
        # FileResponse, so range requests are honoured — which is what iOS insists on.
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _async_wait_for_playlist(hass: HomeAssistant, session: _HlsStream) -> Diagnosis | None:
    """Wait until the playlist names enough segments to start playing.

    ffmpeg writes the playlist only once a segment is complete, and a player handed a
    playlist with a single segment in it can stall waiting for the next one.

    Returns None once there is enough to play, and otherwise why there never was. Note that
    the budget here is `FIRST_OUTPUT_TIMEOUT` and not the four seconds of content being
    waited for: a recorder that has to seek before it can send anything is expected, and
    only something well past that is worth calling a failure.
    """
    path = session.directory / HLS_PLAYLIST
    init = session.directory / HLS_INIT
    started = time.monotonic()
    deadline = started + FIRST_OUTPUT_TIMEOUT
    cpu_before = await hass.async_add_executor_job(_cpu_seconds, session.process.pid)

    def _state() -> tuple[int, bool]:
        """How many segments are listed, and whether ffmpeg wrote a header at all.

        The header is the tell that separates a recorder which never answered from one
        that answered and is dribbling: ffmpeg writes it as soon as it knows the codec,
        long before the first segment is complete.
        """
        try:
            segments = path.read_text(errors="replace").count("#EXTINF")
        except OSError:
            segments = 0
        return segments, init.is_file()

    while True:
        segments, opened = await hass.async_add_executor_job(_state)
        if segments >= HLS_MIN_SEGMENTS:
            return None
        exited = session.process.returncode is not None
        # Something is better than nothing from a converter that has stopped: there will be
        # no more, and one segment still plays.
        if exited and segments > 0:
            return None
        if exited or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.25)

    elapsed = time.monotonic() - started
    cpu_after = await hass.async_add_executor_job(_cpu_seconds, session.process.pid)
    return _diagnose_no_output(
        session,
        elapsed=elapsed,
        load=_cpu_load(cpu_before, cpu_after, elapsed),
        opened=opened,
        progress=f"{segments} of the {HLS_MIN_SEGMENTS} segments needed to start",
    )
