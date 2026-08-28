"""Getting one clip's bytes out of the recorder.

There are two kinds of camera and they want opposite treatment, so the route is chosen per
event from what the search actually returns rather than from configuration:

* **A camera recording on events** has already made the clip. Its file starts before the
  detection — the camera's own pre-record buffer is written into it — and ends when the event
  does. Measured on an RLN8-410: a detection at 19:50:10 produced a recording beginning
  19:50:02, findable 20 seconds later, still growing while the event continued. Nothing to
  cut: fetch that file whole from the download endpoint, which serves MP4 at wire speed.

* **A camera recording 24/7** buries the event inside a segment half an hour long. Uploading
  that to keep twenty seconds would waste the quota and hammer the recorder, so the clip is
  cut out. Playback can be started at any offset server-side, which means only the clip's
  bytes cross the network — but the recorder paces playback at roughly the speed the footage
  was filmed, so this route is as slow as the clip is long, and it needs ffmpeg to put the
  FLV it serves into MP4.

The first route needs nothing installed and finishes in seconds; the second is the fallback.
Which applies is decided by comparing the recording's length to the clip we want.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil

from homeassistant.core import HomeAssistant

from ..redact import scrub_credentials
from ..tls import ffmpeg_tls_args

_LOGGER = logging.getLogger(__name__)

# A recording no longer than the clip plus this is taken as "the file *is* the event", and
# uploaded whole. Beyond it, the event is a fragment of something longer and gets cut out.
WHOLE_FILE_SLACK_SECONDS = 45.0

# ffmpeg is given the clip's length plus this much wall clock before it is killed. Playback
# arrives at about real time, so a clip cannot legitimately take much longer than it lasts.
FFMPEG_GRACE_SECONDS = 60.0


class FetchError(Exception):
    """Raised when a clip's bytes could not be obtained."""


class FfmpegMissingError(FetchError):
    """Raised when cutting is needed but no ffmpeg is installed."""


def async_ffmpeg_binary(hass: HomeAssistant) -> str | None:
    """Return the ffmpeg to use, preferring the one Home Assistant is configured with.

    Only the 24/7 route needs it. Everything else works on an installation without ffmpeg at
    all, which is most of the point of preferring the recorder's own files.
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


def wants_whole_file(recording_seconds: float, clip_seconds: float) -> bool:
    """Whether the recording is close enough to the clip to upload as it stands.

    Deliberately a comparison of what was found against what was wanted, rather than a
    judgement about the camera: a camera can be switched from event to continuous recording
    without telling anyone, and this notices per event.
    """
    if recording_seconds <= 0:
        return False
    return recording_seconds <= clip_seconds + WHOLE_FILE_SLACK_SECONDS


async def async_read_stream(response, limit: int) -> bytes:
    """Drain an aiohttp response into memory, refusing to exceed `limit`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(65536):
        total += len(chunk)
        if total > limit:
            raise FetchError(f"the clip passed {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def async_cut_with_ffmpeg(
    binary: str,
    source_url: str,
    seconds: float,
    limit: int,
    *,
    input_seek: int = 0,
    verify_tls: bool = False,
) -> bytes:
    """Copy `seconds` of a stream into a fragmented MP4, in memory.

    `-c copy` so the footage is untouched — this is a change of container, not a re-encode —
    and the fragmented flags because a plain MP4 needs to seek backwards to finish its index,
    which a pipe cannot do.

    The process is given a deadline and killed if it misses it: an NVR that stops sending
    mid-clip would otherwise leave ffmpeg waiting for bytes that never come, and a stuck
    subprocess inside Home Assistant is worse than a missing clip.

    `input_seek` is for a recorder that cannot seek its own playback and hands over the
    whole file instead: the clip then has to be found within it here. Before `-i` so the
    reader skips ahead rather than decoding everything up to the window.

    `verify_tls` follows the option, so this pull agrees with the aiohttp ones about the
    recorder's certificate. Its default matches the option's: ffmpeg does not verify.
    """
    args = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *ffmpeg_tls_args(source_url, verify_tls=verify_tls),
        *(("-ss", f"{input_seek:.3f}") if input_seek > 0 else ()),
        "-i",
        source_url,
        "-t",
        f"{seconds:.3f}",
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    deadline = seconds + FFMPEG_GRACE_SECONDS
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=deadline)
    except TimeoutError as err:
        process.kill()
        await process.wait()
        raise FetchError(f"ffmpeg did not finish within {deadline:.0f}s") from err

    if process.returncode != 0:
        # Scrubbed because ffmpeg quotes the input URL, which on the `/flv` playback route
        # carries the recorder's own username and password.
        detail = scrub_credentials(stderr.decode(errors="replace")[:300].strip())
        raise FetchError(f"ffmpeg failed: {detail or f'exit {process.returncode}'}")
    if not stdout:
        raise FetchError("ffmpeg produced no output")
    if len(stdout) > limit:
        raise FetchError(f"the clip passed {limit} bytes")
    _LOGGER.debug("Cut %.1fs into %s bytes of MP4", seconds, len(stdout))
    return stdout


def stable(previous_end: dt.datetime | None, current_end: dt.datetime) -> bool:
    """Whether a growing recording has stopped growing.

    An event camera keeps extending its recording for as long as it sees something, so a
    clip is only complete once two consecutive searches agree on where it ends.
    """
    return previous_end is not None and current_end <= previous_end
