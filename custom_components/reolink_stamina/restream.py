"""Beta: converting a recording server-side, when and only when the browser cannot.

The normal playback route needs nothing installed and costs nothing: the recorder serves
FLV, the browser demuxes it through Media Source Extensions, and Home Assistant only
forwards bytes (see flv_proxy.py). Two kinds of viewer fall outside that.

* **A recorder encoding H.265.** Reolink's main stream usually is, and on some cameras the
  sub stream is too. Chrome and Firefox refuse HEVC in Media Source Extensions, so the
  bytes arrive perfectly and nothing is drawn.
* **An iPhone.** iOS has no `MediaSource` at all — the demuxer cannot run — and Safari
  will not play a progressive stream whose length is unknown and whose server ignores
  range requests, which is what any live-paced route is. HLS is the only thing it takes.

Neither of those necessarily needs re-encoding, and re-encoding is by far the most
expensive thing this integration can be asked to do. So the panel works down a ladder and
stops at the first rung that plays, which for most devices is the first or second:

1. **Pass through** — the recorder's FLV, demuxed in the browser. No server work at all.
2. **Remux** (`copy`) — ffmpeg changes the container and nothing else. This is what an
   iPhone needs for an H.264 recording: the phone's own hardware decoder does the work,
   Home Assistant only repackages. Cheap enough to run on any machine.
3. **Re-encode** — only for a codec the device itself cannot decode, which in practice
   means H.265 on Chrome or Firefox. Hardware encoding is used where the machine has it.

Two containers, chosen by what the browser can play:

* `mp4` — fragmented MP4 straight down one chunked response. Plays natively in Chrome,
  Firefox and desktop Safari, and needs no session state at all.
* `hls` — a playlist and fragmented-MP4 segments written to a temporary directory and
  served from it. For iOS, where nothing else works.

**One at a time.** A single slot for the whole integration: starting a stream stops
whichever one was running. Seeking reopens the stream at another offset, so a viewer
routinely replaces their own stream, and the alternative to replacing it is refusing the
seek. Cloud sync's own ffmpeg runs are separate from this — they are short, bounded, and
already queued one per recorder.
"""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from typing import Any, Final

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .ffmpeg import async_ffmpeg_binary
from .flv_proxy import async_playback_source
from .reolink_registry import DeviceUnavailableError, ReolinkIncompatibleError

_LOGGER = logging.getLogger(__name__)

# What the recording is put into.
FORMAT_MP4: Final = "mp4"
FORMAT_HLS: Final = "hls"
RESTREAM_FORMATS: Final = (FORMAT_MP4, FORMAT_HLS)

# How much work is done to it. `copy` repackages; `encode` decodes and re-encodes.
MODE_COPY: Final = "copy"
MODE_ENCODE: Final = "encode"
RESTREAM_MODES: Final = (MODE_COPY, MODE_ENCODE)

# Where the manager lives. Kept out of the integration's runtime data so a stream that
# outlives a reload can still be found and stopped.
_MANAGER_KEY: Final = f"{DOMAIN}_restream_manager"

# A full-sensor Reolink stream is 4608x1728, and re-encoding that in real time is beyond
# any machine Home Assistant typically runs on. Capped by height, aspect kept, and only
# ever downwards. Applies to re-encoding alone: a remux never touches the picture, and
# neither does any download.
MAX_HEIGHT: Final = 1080

# How long to wait for the first output before giving up on a stream. The recorder takes a
# second or two to answer and ffmpeg a moment to open the input; well past that, something
# is wrong and saying so beats a spinner that never resolves.
FIRST_OUTPUT_TIMEOUT: Final = 30.0

_CHUNK: Final = 65536
# Enough of ffmpeg's complaint to be useful in the panel and the log, and no more.
_STDERR_LIMIT: Final = 4096

HLS_SEGMENT_SECONDS: Final = 2
# Apple's own guidance is to have a few segments of content before starting; two is the
# compromise between that and how long the panel sits on a spinner.
HLS_MIN_SEGMENTS: Final = 2
HLS_PLAYLIST: Final = "index.m3u8"
HLS_INIT: Final = "init.mp4"
# Nobody has asked for a segment in this long: the viewer closed the tab or walked away,
# and the recorder should stop being pulled from.
HLS_IDLE_TIMEOUT: Final = 60.0
# A ceiling however diligently it is being read, so a forgotten tab cannot stream for ever.
HLS_MAX_SESSION_SECONDS: Final = 3600.0
_HLS_SWEEP_INTERVAL: Final = 10.0
# The names ffmpeg writes, and nothing else: this is what stops a session token being used
# to read the rest of the filesystem.
_HLS_FILE = re.compile(r"^[A-Za-z0-9_-]+\.(m3u8|mp4|m4s)$")


@dataclass(frozen=True, slots=True)
class Encoder:
    """One way of producing H.264, and what ffmpeg needs to be told to use it."""

    name: str
    # Before -i, e.g. the VAAPI device to open.
    input_args: tuple[str, ...] = ()
    # After the codec is chosen.
    output_args: tuple[str, ...] = ()
    # Appended to the filter chain, so scaling still happens in software first and the
    # frames are handed to the encoder in the form it wants.
    filters: tuple[str, ...] = ()
    # False for libx264, which is always available and never needs remembering as broken.
    hardware: bool = True


SOFTWARE_ENCODER: Final = Encoder(
    name="libx264",
    # veryfast rather than ultrafast: playback is paced by the recorder at roughly real
    # time, so there is CPU budget to spend on not tripling the bitrate.
    output_args=("-preset", "veryfast", "-tune", "zerolatency", "-crf", "23"),
    hardware=False,
)

# Tried in this order. Every one of them accepts frames from system memory, which is what
# keeps one filter chain serving all of them — VAAPI being the exception that has to
# upload, and says so.
#
# Each is given an explicit bitrate: several hardware encoders default to something absurd
# for the resolution (h264_v4l2m2m to 200 kbit/s), which reads as a broken picture rather
# than as a wrong default.
_HARDWARE_ENCODERS: Final = (
    # Apple silicon and Intel Macs.
    Encoder(name="h264_videotoolbox", output_args=("-b:v", "4M")),
    # Intel iGPU, which is what most mini-PC Home Assistant boxes have.
    Encoder(name="h264_qsv", output_args=("-b:v", "4M")),
    Encoder(
        name="h264_vaapi",
        input_args=("-vaapi_device", "/dev/dri/renderD128"),
        output_args=("-b:v", "4M"),
        filters=("format=nv12", "hwupload"),
    ),
    Encoder(name="h264_nvenc", output_args=("-b:v", "4M")),
    # Rockchip boards, then the Raspberry Pi 4's own encoder.
    Encoder(name="h264_rkmpp", output_args=("-b:v", "4M")),
    Encoder(name="h264_v4l2m2m", output_args=("-b:v", "4M")),
)

# Hardware encoders not worth trying at all without a render node present.
_NEEDS_DRI: Final = frozenset({"h264_qsv", "h264_vaapi"})


def async_restream_path(
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start_id: str,
    playback_id: str,
    seek: int,
    mode: str = MODE_COPY,
) -> str:
    """Return the unsigned path that serves one recording as fragmented MP4."""
    encoded = urlsafe_b64encode(filename.encode()).decode()
    return (
        f"/api/reolink_stamina/restream/{mode}/{entry_id}/{channel}/{stream}"
        f"/{encoded}/{start_id}/{playback_id}/{max(0, int(seek))}"
    )


def async_hls_path(token: str) -> str:
    """Return the path of a live HLS session's playlist."""
    return f"/api/reolink_stamina/hls/{token}/{HLS_PLAYLIST}"


# --------------------------------------------------------------------- encoder choice


def _available_encoders(output: str) -> set[str]:
    """Parse `ffmpeg -encoders` into the set of encoder names it lists.

    Lines look like ` V....D h264_qsv    H.264 ...`, so the name is the second field of
    any line whose flags begin with V for video. The listing opens with a legend in the same
    shape — ` V..... = Video` — which is why the name has to look like a name.
    """
    found: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"\s*([A-Z.]{6})\s+([A-Za-z0-9_]+)", line)
        if match and match.group(1).startswith("V"):
            found.add(match.group(2))
    return found


async def async_choose_encoder(hass: HomeAssistant, binary: str) -> Encoder:
    """Return the best H.264 encoder this machine can actually use.

    Probed once and remembered, and only ever asked for when something is about to be
    re-encoded. Anything that has failed in the field is skipped: a GPU that is present
    but not working must not cost every subsequent clip its playback.
    """
    manager = async_get_manager(hass)
    if manager.encoder is not None:
        return manager.encoder

    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-hide_banner",
            "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        available = _available_encoders(stdout.decode(errors="replace"))
    except Exception:
        _LOGGER.debug("Could not list ffmpeg encoders; using software", exc_info=True)
        available = set()

    has_dri = await hass.async_add_executor_job(Path("/dev/dri/renderD128").exists)

    chosen = SOFTWARE_ENCODER
    for candidate in _HARDWARE_ENCODERS:
        if candidate.name not in available or candidate.name in manager.failed_encoders:
            continue
        if candidate.name in _NEEDS_DRI and not has_dri:
            continue
        chosen = candidate
        break

    _LOGGER.info("Reolink Stamina will re-encode playback with %s", chosen.name)
    manager.encoder = chosen
    return chosen


def build_args(
    binary: str,
    source_url: str,
    *,
    mode: str,
    output_format: str,
    encoder: Encoder = SOFTWARE_ENCODER,
    directory: Path | None = None,
) -> list[str]:
    """Build the ffmpeg command for one stream.

    Kept pure so the shape of it can be asserted without a recorder or a subprocess.

    Audio is converted whichever mode this is: Reolink recorders variously serve AAC,
    ADPCM and G.711, and only the first of those can go into MP4 at all. It is one mono
    channel, so it costs nothing next to the video.

    `-hwaccel auto` covers the expensive half of a re-encode on hardware that can decode
    H.265 itself, and falls back to software silently where it cannot. The filters run in
    system memory either way, so one chain serves every encoder.
    """
    args = [binary, "-hide_banner", "-loglevel", "error", "-nostdin"]

    if mode == MODE_ENCODE:
        args += ["-hwaccel", "auto", *encoder.input_args]

    # The recorder's FLV carries no usable timestamps at the start of a seek.
    args += ["-fflags", "+genpts", "-i", source_url]

    if mode == MODE_ENCODE:
        filters = [f"scale=-2:min(ih\\,{MAX_HEIGHT})", *encoder.filters]
        args += ["-vf", ",".join(filters), "-c:v", encoder.name, *encoder.output_args]
    else:
        args += ["-c:v", "copy"]

    args += ["-c:a", "aac", "-ac", "1"]

    if output_format == FORMAT_HLS:
        if directory is None:
            raise ValueError("HLS output needs a directory to write into")
        if mode == MODE_ENCODE:
            # Segments have to start on a keyframe, and the recorder's own interval is
            # longer than one segment. Copying cannot ask for keyframes, so there the
            # segment length follows whatever the recording already has.
            args += ["-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})"]
        args += [
            "-f",
            "hls",
            "-hls_time",
            str(HLS_SEGMENT_SECONDS),
            # A sliding window: nothing accumulates, and seeking is server-side anyway.
            "-hls_list_size",
            "6",
            "-hls_flags",
            "delete_segments+independent_segments+temp_file",
            # Fragmented MP4 rather than MPEG-TS, because it is the only HLS container
            # that reliably carries H.265 — which is the whole point of copying rather
            # than re-encoding for a device that can decode it.
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            HLS_INIT,
            "-hls_segment_filename",
            str(directory / "s%05d.m4s"),
            str(directory / HLS_PLAYLIST),
        ]
        return args

    args += [
        # A plain MP4 rewinds to write its index, which a pipe cannot do.
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]
    return args


# ------------------------------------------------------------------------- sessions


class _Stream:
    """A running ffmpeg, and what is needed to stop it or explain why it stopped."""

    def __init__(self, process: asyncio.subprocess.Process, label: str, encoder: Encoder) -> None:
        """Start draining stderr immediately, so a full pipe cannot stall ffmpeg."""
        self.process = process
        self.label = label
        self.encoder = encoder
        self._stderr = bytearray()
        self._drain = asyncio.create_task(self._async_read_stderr())

    async def _async_read_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        try:
            while chunk := await stderr.read(1024):
                if len(self._stderr) < _STDERR_LIMIT:
                    self._stderr.extend(chunk)
        except Exception:
            # The process going away is how this normally ends.
            _LOGGER.debug("Stopped reading ffmpeg's output for %s", self.label)

    @property
    def error_detail(self) -> str:
        """What ffmpeg said, trimmed to something worth showing a user."""
        return self._stderr.decode(errors="replace").strip()[:300]

    async def async_stop(self) -> None:
        """Kill ffmpeg and wait for it, so nothing is left pulling from the recorder."""
        self._drain.cancel()
        # Logged even when the stream was watched happily: a device whose recordings will not
        # play in one browser and will in another leaves its explanation here, and there is
        # no other way to see what ffmpeg made of the input.
        if self._stderr:
            _LOGGER.debug("ffmpeg said of %s: %s", self.label, self.error_detail)
        if self.process.returncode is not None:
            return
        try:
            self.process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except TimeoutError:
            _LOGGER.debug("ffmpeg did not exit after being killed")


class _HlsStream(_Stream):
    """An HLS session: the same ffmpeg, plus the directory it writes into."""

    def __init__(
        self,
        hass: HomeAssistant,
        process: asyncio.subprocess.Process,
        label: str,
        encoder: Encoder,
        token: str,
        directory: Path,
    ) -> None:
        """Start the idle watchdog along with the stream."""
        super().__init__(process, label, encoder)
        self.hass = hass
        self.token = token
        self.directory = directory
        self.started_at = time.monotonic()
        self.last_read = self.started_at
        self._watchdog = asyncio.create_task(self._async_watch())

    def touch(self) -> None:
        """Note that the player is still reading."""
        self.last_read = time.monotonic()

    async def _async_watch(self) -> None:
        """Stop a session nobody is reading, and one that has run long enough."""
        try:
            while True:
                await asyncio.sleep(_HLS_SWEEP_INTERVAL)
                now = time.monotonic()
                if now - self.last_read > HLS_IDLE_TIMEOUT:
                    _LOGGER.debug("Restream %s idle; stopping", self.label)
                    break
                if now - self.started_at > HLS_MAX_SESSION_SECONDS:
                    _LOGGER.debug("Restream %s reached its time limit; stopping", self.label)
                    break
        except asyncio.CancelledError:
            return
        await async_get_manager(self.hass).async_release(self)

    async def async_stop(self) -> None:
        """Stop ffmpeg, then delete everything it wrote."""
        self._watchdog.cancel()
        await super().async_stop()
        directory = self.directory
        try:
            await self.hass.async_add_executor_job(
                lambda: shutil.rmtree(directory, ignore_errors=True)
            )
        except Exception:
            _LOGGER.debug("Could not remove %s", directory, exc_info=True)


class RestreamManager:
    """The integration's one streaming slot.

    Deliberately a slot rather than a pool. Converting is the most expensive thing this
    integration can be asked to do, and a browser that reconnects — a seek, a reopened
    clip, a reloaded panel — would otherwise leave the previous one running. Whoever
    starts a stream gets it, and whoever had it loses it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Hold no stream, and no opinion about encoders until one is probed."""
        self.hass = hass
        self.encoder: Encoder | None = None
        self.failed_encoders: set[str] = set()
        self._current: _Stream | None = None
        self._lock = asyncio.Lock()

    async def async_claim(self, stream: _Stream) -> None:
        """Take the slot for `stream`, stopping whatever held it."""
        async with self._lock:
            previous = self._current
            self._current = stream
        if previous is not None:
            _LOGGER.debug("Replacing restream %s with %s", previous.label, stream.label)
            await previous.async_stop()

    async def async_release(self, stream: _Stream) -> None:
        """Stop `stream` and give up the slot, if it still holds it."""
        async with self._lock:
            if self._current is stream:
                self._current = None
        await stream.async_stop()

    def holds(self, stream: _Stream) -> bool:
        """Whether `stream` is still the one running.

        Asked before blaming a hardware encoder for producing nothing: a stream that was
        evicted mid-startup produced nothing because it was killed, which says nothing at
        all about the encoder.
        """
        return self._current is stream

    def hls_session(self, token: str) -> _HlsStream | None:
        """Return the live HLS session for a token, if that is what is running."""
        current = self._current
        if isinstance(current, _HlsStream) and current.token == token:
            return current
        return None

    def note_encoder_failure(self, encoder: Encoder) -> None:
        """Remember a hardware encoder that produced nothing, and stop choosing it."""
        if not encoder.hardware:
            return
        _LOGGER.warning(
            "Reolink Stamina could not re-encode with %s; using software encoding from now on",
            encoder.name,
        )
        self.failed_encoders.add(encoder.name)
        self.encoder = None

    async def async_stop(self) -> None:
        """Stop whatever is running. Called when the integration unloads."""
        async with self._lock:
            current = self._current
            self._current = None
        if current is not None:
            await current.async_stop()


@callback
def async_get_manager(hass: HomeAssistant) -> RestreamManager:
    """Return the restream manager, creating it on first use."""
    manager = hass.data.get(_MANAGER_KEY)
    if manager is None:
        manager = RestreamManager(hass)
        hass.data[_MANAGER_KEY] = manager
    return manager


async def async_shutdown(hass: HomeAssistant) -> None:
    """Stop any running stream, on unload."""
    manager = hass.data.get(_MANAGER_KEY)
    if manager is not None:
        await manager.async_stop()


# ------------------------------------------------------------------------- starting


@callback
def async_beta_enabled(hass: HomeAssistant) -> bool:
    """Whether adaptive playback is switched on right now."""
    data = hass.data.get(DOMAIN)
    return bool(data is not None and data.options.beta_restream)


class RestreamError(Exception):
    """Raised when a stream could not be started."""


class FfmpegUnavailableError(RestreamError):
    """Raised when there is no ffmpeg to convert with."""


async def _async_spawn(
    hass: HomeAssistant,
    source_url: str,
    *,
    label: str,
    mode: str,
    output_format: str,
    directory: Path | None = None,
) -> tuple[asyncio.subprocess.Process, Encoder]:
    """Start ffmpeg for one stream, and say which encoder it was given."""
    binary = async_ffmpeg_binary(hass)
    if binary is None:
        raise FfmpegUnavailableError(
            "Adaptive playback needs ffmpeg, and none was found. It ships with Home "
            "Assistant OS, Container and Supervised installations."
        )

    # Only a re-encode has an encoder to choose, and only it pays for the probe.
    encoder = await async_choose_encoder(hass, binary) if mode == MODE_ENCODE else SOFTWARE_ENCODER
    args = build_args(
        binary,
        source_url,
        mode=mode,
        output_format=output_format,
        encoder=encoder,
        directory=directory,
    )
    _LOGGER.debug(
        "Restreaming %s (%s, %s)",
        label,
        output_format,
        encoder.name if mode == MODE_ENCODE else "copy",
    )
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=(
            asyncio.subprocess.PIPE if output_format == FORMAT_MP4 else asyncio.subprocess.DEVNULL
        ),
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    return process, encoder


async def async_start_hls(
    hass: HomeAssistant,
    entry_id: str,
    channel: int,
    stream: str,
    filename: str,
    start_id: str,
    playback_id: str,
    seek: int,
    mode: str = MODE_COPY,
) -> str:
    """Start an HLS session for one recording and return its token.

    Started here rather than on the first request for the playlist, so the panel is handed
    a URL it can give straight to a video element — an iPhone hands playback to the system
    player, which will not follow anything more elaborate.
    """
    source = await async_playback_source(
        hass, entry_id, channel, stream, filename, start_id, playback_id, seek
    )
    token = secrets.token_urlsafe(24)
    label = f"{entry_id}/{channel}/{stream}@{seek}s hls"
    directory = Path(
        await hass.async_add_executor_job(lambda: tempfile.mkdtemp(prefix="reolink_stamina_"))
    )

    try:
        process, encoder = await _async_spawn(
            hass,
            source,
            label=label,
            mode=mode,
            output_format=FORMAT_HLS,
            directory=directory,
        )
    except Exception:
        await hass.async_add_executor_job(lambda: shutil.rmtree(directory, ignore_errors=True))
        raise

    session = _HlsStream(hass, process, label, encoder, token, directory)
    await async_get_manager(hass).async_claim(session)
    return token


# ---------------------------------------------------------------------------- views


class ReolinkStaminaRestreamView(HomeAssistantView):
    """Serve a recording as fragmented MP4, repackaged or re-encoded."""

    url = (
        "/api/reolink_stamina/restream/{mode}/{entry_id}/{channel}/{stream}"
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

        if not async_beta_enabled(hass):
            return web.Response(status=404, text="Adaptive playback is not enabled")
        if mode not in RESTREAM_MODES:
            return web.Response(status=400, text="Unknown conversion mode")

        try:
            name = urlsafe_b64decode(filename.encode()).decode()
            channel_no = int(channel)
            seek_seconds = max(0, int(seek))
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="Malformed recording reference")

        try:
            source = await async_playback_source(
                hass, entry_id, channel_no, stream, name, start_id, playback_id, seek_seconds
            )
        except (DeviceUnavailableError, ReolinkIncompatibleError) as err:
            return web.Response(status=404, text=str(err))
        except Exception as err:
            _LOGGER.debug("Could not resolve a playback URL", exc_info=True)
            return web.Response(status=502, text=f"Could not open the recording: {err}")

        label = f"{entry_id}/{channel_no}/{stream}@{seek_seconds}s mp4"
        manager = async_get_manager(hass)

        try:
            process, encoder = await _async_spawn(
                hass, source, label=label, mode=mode, output_format=FORMAT_MP4
            )
        except FfmpegUnavailableError as err:
            return web.Response(status=501, text=str(err))
        except Exception as err:
            _LOGGER.debug("Could not start ffmpeg", exc_info=True)
            return web.Response(status=502, text=f"Could not start the conversion: {err}")

        active = _Stream(process, label, encoder)
        await manager.async_claim(active)

        # Wait for the first bytes before answering, so a conversion that fails outright is
        # an error the panel can read rather than an empty video and a silent log line.
        try:
            first = await asyncio.wait_for(
                process.stdout.read(_CHUNK), timeout=FIRST_OUTPUT_TIMEOUT
            )
        except Exception:
            # Timed out, or the process was killed because another stream took the slot.
            first = b""

        if not first:
            detail = active.error_detail
            evicted = not manager.holds(active)
            await manager.async_release(active)
            if mode == MODE_ENCODE and not evicted:
                # A hardware encoder that cannot run is worth never choosing again; the
                # panel retries, and the retry uses software.
                manager.note_encoder_failure(encoder)
            _LOGGER.warning("Restreaming %s produced nothing: %s", label, detail or "no output")
            return web.Response(
                status=502,
                text=f"Could not convert this recording: {detail or 'ffmpeg produced no output'}",
            )

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
            if not await _async_wait_for_playlist(hass, session):
                detail = session.error_detail
                _LOGGER.warning("HLS session %s produced no playlist: %s", session.label, detail)
                evicted = not manager.holds(session)
                await manager.async_release(session)
                if not evicted:
                    manager.note_encoder_failure(session.encoder)
                return web.Response(
                    status=502,
                    text=f"Could not convert this recording: {detail or 'no output'}",
                )
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


async def _async_wait_for_playlist(hass: HomeAssistant, session: _HlsStream) -> bool:
    """Wait until the playlist names enough segments to start playing.

    ffmpeg writes the playlist only once a segment is complete, and a player handed a
    playlist with a single segment in it can stall waiting for the next one.
    """
    path = session.directory / HLS_PLAYLIST
    deadline = time.monotonic() + FIRST_OUTPUT_TIMEOUT

    def _segments() -> int:
        try:
            return path.read_text(errors="replace").count("#EXTINF")
        except OSError:
            return 0

    while time.monotonic() < deadline:
        if await hass.async_add_executor_job(_segments) >= HLS_MIN_SEGMENTS:
            return True
        if session.process.returncode is not None:
            # ffmpeg gave up; no amount of waiting will produce more.
            return await hass.async_add_executor_job(_segments) > 0
        await asyncio.sleep(0.25)
    return False
