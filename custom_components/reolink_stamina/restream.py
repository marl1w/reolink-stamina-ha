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
from collections import deque
import contextlib
from dataclasses import dataclass
import logging
import os
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
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .ffmpeg import async_ffmpeg_binary
from .flv_proxy import async_playback_source, scrub_credentials
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
# How much of it is quoted into a log line and the diagnostics download. Kept well short of
# what is captured, because the whole of it is what gets *classified* and only the head of it
# is worth reading — a distinction this did not use to draw, at the cost of every diagnosis
# on a machine whose first few lines are always the same noise. See `error_text`.
_DETAIL_LIMIT: Final = 600
# How many failed conversions are remembered for the panel and the diagnostics download.
# Enough to show a pattern — a hardware encoder failing its way down the list, a recorder
# that is slow every time — without keeping a session's worth of noise.
_FAILURE_HISTORY: Final = 10

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
# What every session directory is named after, so the sweep at setup can recognise one and
# nothing else in the temporary filesystem is ever a candidate for removal.
SESSION_PREFIX: Final = "reolink_stamina_"
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


# How long a candidate gets to encode six frames of colour bars before it counts as broken.
# A budget for a wedged driver, not for the work: anything that can do this at all does it in
# well under a second.
_ENCODER_TEST_TIMEOUT: Final = 20.0


async def _async_encoder_works(binary: str, encoder: Encoder) -> bool:
    """Whether this machine can actually encode with `encoder`, tested rather than assumed.

    `ffmpeg -encoders` lists what the binary was *compiled* with, which on the builds Home
    Assistant ships is very nearly every hardware encoder in existence. It says nothing about
    whether the driver behind one is installed, whether the device is real, or whether a
    virtual machine has been handed something it can only pretend with — and a render node
    exists on machines whose graphics device is a paravirtualised framebuffer with no media
    engine at all, which is exactly the case that used to get through this.

    So the candidate is asked to encode something. A quarter of a second of colour at 640x360
    into nothing costs nothing where it works, and where it does not it fails here — once, at
    startup, in the debug log — rather than costing a viewer their clip.
    """
    args = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *encoder.input_args,
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360:r=25:d=0.25",
        # The same chain a real conversion builds, so an encoder that cannot take frames in
        # this form fails the test for the reason it would fail the clip.
        *(["-vf", ",".join(encoder.filters)] if encoder.filters else []),
        "-c:v",
        encoder.name,
        *encoder.output_args,
        "-f",
        "null",
        "-",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        _LOGGER.debug("Could not run the %s encoder test", encoder.name, exc_info=True)
        return False

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=_ENCODER_TEST_TIMEOUT)
    except TimeoutError:
        # A driver that hangs is no more usable than one that refuses, and one left running
        # holds the render node against everything else on the machine.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        _LOGGER.debug("The %s encoder test did not finish", encoder.name)
        return False

    if process.returncode == 0:
        return True
    _LOGGER.debug(
        "This machine cannot encode with %s: %s",
        encoder.name,
        stderr.decode(errors="replace").strip()[:_DETAIL_LIMIT] or f"exit {process.returncode}",
    )
    return False


async def async_choose_encoder(hass: HomeAssistant, binary: str) -> Encoder:
    """Return the best H.264 encoder this machine can actually use.

    Probed once and remembered, and only ever asked for when something is about to be
    re-encoded. Anything that has failed in the field is skipped: a GPU that is present
    but not working must not cost every subsequent clip its playback.

    Each candidate is listed, then tried. Listing alone was what this used to do, and on a
    machine where the listing is right and the hardware is not it cost three clips — one per
    hardware encoder — every time Home Assistant restarted, because the only way an encoder
    got onto the broken list was a viewer discovering it. Trying costs a second, once.
    """
    manager = async_get_manager(hass)
    # Held across the whole probe, not just the read of it: two clips opened together would
    # otherwise both run the tests, and the loser's work is pure waste on the very machines
    # least able to afford it.
    async with manager.encoder_lock:
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
            if not await _async_encoder_works(binary, candidate):
                # Remembered exactly like a failure in the field, because it is the same fact
                # arrived at more cheaply: this machine cannot use it, and nothing about that
                # changes until it is restarted onto different hardware.
                manager.failed_encoders.add(candidate.name)
                continue
            chosen = candidate
            break

        if chosen.hardware:
            _LOGGER.info("Reolink Stamina will re-encode playback with %s", chosen.name)
        else:
            # Worth a sentence rather than a name: this is the slow path, and on a machine
            # with a graphics device that looks usable it is a surprise worth explaining.
            _LOGGER.info(
                "Reolink Stamina will re-encode playback in software (%s)%s",
                chosen.name,
                (
                    f"; no hardware encoder on this machine could be used "
                    f"({', '.join(sorted(manager.failed_encoders))})"
                    if manager.failed_encoders
                    else ""
                ),
            )
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
    channel, so it costs nothing next to the video — and being converted, it is also the
    track that has to be told what to do with the recorder's timestamps at a seek.

    `-hwaccel auto` covers the expensive half of a re-encode on hardware that can decode
    H.265 itself, and falls back to software silently where it cannot. The filters run in
    system memory either way, so one chain serves every encoder.
    """
    # `warning` rather than `error`: the lines that explain a conversion which starts and
    # then falls behind are warnings, not errors, and they are the difference between a
    # diagnosis and a shrug. What is kept of them is capped at `_STDERR_LIMIT` regardless.
    args = [binary, "-hide_banner", "-loglevel", "warning", "-nostdin"]

    if mode == MODE_ENCODE:
        args += ["-hwaccel", "auto", *encoder.input_args]

    # The recorder's FLV carries no usable timestamps at the start of a seek.
    args += ["-fflags", "+genpts", "-i", source_url]

    if mode == MODE_ENCODE:
        filters = [f"scale=-2:min(ih\\,{MAX_HEIGHT})", *encoder.filters]
        args += ["-vf", ",".join(filters), "-c:v", encoder.name, *encoder.output_args]
    else:
        args += ["-c:v", "copy"]

    # `aresample=async=1` on top of that, for the audio alone. A recording opened part-way
    # through arrives with timestamps that step backwards over the first few packets, and the
    # AAC encoder says so — "Queue input is backward in time" — and then encodes them anyway,
    # against a clock that no longer matches the video's. Padding or trimming the gap instead
    # keeps one monotonic audio timeline without touching where it starts, which is what keeps
    # it in step with a video track that is being copied rather than re-encoded.
    args += ["-af", "aresample=async=1", "-c:a", "aac", "-ac", "1"]

    if output_format == FORMAT_HLS:
        if directory is None:
            raise ValueError("HLS output needs a directory to write into")
        if mode == MODE_ENCODE:
            # Segments have to start on a keyframe, and the recorder's own interval is
            # longer than one segment. Copying cannot ask for keyframes, so there the
            # segment length follows whatever the recording already has.
            args += ["-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})"]
        else:
            # `hvc1` rather than whatever ffmpeg would have picked. Repackaging exists so a
            # device can use its own decoder, and Safari — the device that most needs it —
            # refuses HEVC in fragmented MP4 tagged `hev1`, which is what ffmpeg writes when
            # the source carried no tag of its own to copy. The recorder's FLV never does.
            #
            # Only here, and this is the whole of why: the two muxers disagree about what an
            # inapplicable tag means. The fragmented-MP4 segmenter ignores it and writes
            # `avc1` for an H.264 stream, so it costs the common case nothing — but the plain
            # MP4 muxer *refuses*, with "Tag hvc1 incompatible with output codec id '27'", and
            # writes no header at all. Asking for it on the piped route therefore broke every
            # H.264 remux outright, which is the route Chrome and Firefox use and the one an
            # Apple device never takes. Nothing on that route wants the tag anyway: `hvc1`
            # against `hev1` is a Safari quirk, and Safari is served HLS.
            args += ["-tag:v", "hvc1"]
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


# --------------------------------------------------------------------- why one failed

# How much processor a converter has to be using before it counts as doing work rather than
# waiting to be sent something, and how little before it counts as idle. Between the two,
# the evidence does not say which side is the bottleneck and this declines to guess.
_BUSY_LOAD: Final = 0.5
_IDLE_LOAD: Final = 0.1


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """Why a conversion produced too little to play, in terms the viewer can act on.

    This exists because the obvious place to put an explanation — the body of the 502 —
    is read by nobody. Both converted routes hand a URL to a `<video>` element and let the
    browser fetch it, so all the panel ever sees is a numeric `MediaError`. The sentence
    has to travel back over the websocket instead, which is what `code` and `message` are
    for; the rest goes to the log and to diagnostics.
    """

    code: str
    message: str
    # Whether the encoder is to blame, and so worth never choosing again. A machine that is
    # merely too slow is not: falling back from hardware to software would only make it
    # slower, and today any 502 at all disables a working GPU for good.
    encoder_at_fault: bool = False

    def as_dict(self) -> dict[str, str]:
        """Return the shape the panel and the diagnostics download are given."""
        return {"code": self.code, "message": self.message}


# Matched against ffmpeg's own words, in order, and only for failures whose cause is not
# ambiguous. Where it is ambiguous, the numbers in `_diagnose_no_output` decide instead.
_FFMPEG_FAULTS: Final = (
    (
        re.compile(r"no space left|disk full", re.I),
        "no_space",
        "Home Assistant has no temporary space left to write the converted video into. "
        "Restarting Home Assistant clears it, and this is worth reporting as a bug.",
        False,
    ),
    (
        re.compile(r"401 unauthorized|403 forbidden|login failed|authentication", re.I),
        "device_rejected",
        "The recorder refused the request for this recording. Its password may have "
        "changed since Home Assistant last connected to it.",
        False,
    ),
    (
        re.compile(
            r"connection refused|connection timed out|no route to host|"
            r"name or service not known|network is unreachable",
            re.I,
        ),
        "device_unreachable",
        "Home Assistant could not reach the recorder to read this recording, even though "
        "it answered when the clip list was built.",
        False,
    ),
    (
        re.compile(
            r"unknown encoder|cannot load|device creation failed|no device available|"
            r"function not implemented|error initializing output stream|"
            # A device the command asked for by name and did not get. ffmpeg rejects these
            # while parsing its own arguments, so it never reaches the recorder at all — and
            # it says so in words that name no device, which is why they are matched here
            # rather than left to the generic phrases above.
            r"failed to set value .* for option '\w+_device'|error parsing global options|"
            # What the QSV and V4L2 encoders say when the driver behind them is absent.
            r"error initializing an internal mfx session|"
            r"could not find a valid device|no such file or directory.*video",
            re.I,
        ),
        "encoder_unavailable",
        "This machine's hardware video encoder could not be used. Playing the clip again "
        "will re-encode in software instead, which is slower but always works.",
        True,
    ),
    (
        re.compile(r"invalid data found|could not find codec|decoder.*not found", re.I),
        "unreadable_stream",
        "The recorder sent something Home Assistant could not read as video. The other "
        "resolution often works where this one does not.",
        False,
    ),
    (
        re.compile(r"connection reset|end of file|broken pipe|i/o error", re.I),
        "device_stopped",
        "The recorder stopped sending this recording part-way through. Recorders do this "
        "when they are busy serving several streams at once.",
        False,
    ),
)


def _classify_ffmpeg_error(detail: str) -> Diagnosis | None:
    """Turn what ffmpeg said into a sentence, when it said something recognisable."""
    if not detail:
        return None
    for pattern, code, message, encoder_at_fault in _FFMPEG_FAULTS:
        if pattern.search(detail):
            return Diagnosis(code, message, encoder_at_fault)
    return None


# The device types `-hwaccel auto` works through on its way past. It creates every one the
# decoder could possibly use and prints an error for each it cannot, before the encoder has
# said a word — and then, having found nothing, decodes in software and carries on perfectly
# happily. None of it is a failure. All of it looks like one.
#
# It has to be dropped before anything is read from ffmpeg's output, because it arrives first
# and `Device creation failed` is one of the phrases that condemns an encoder. Left in on a
# machine with no working acceleration it was the *only* thing that ever got read: three or
# four lines of it, on every run, filling the quoted extract entirely. Every re-encode that
# produced nothing was therefore diagnosed as a broken encoder — a clip that was merely slow,
# a recorder that stopped sending, a full disk — and each such diagnosis cost a hardware
# encoder its place in the list for good.
_PROBE_DEVICES: Final = frozenset(
    {
        "vaapi",
        "vdpau",
        "vulkan",
        "cuda",
        "qsv",
        "opencl",
        "drm",
        "d3d11va",
        "d3d12va",
        "dxva2",
        "videotoolbox",
        "mediacodec",
    }
)
# ffmpeg tags every line with the component that wrote it: `[VAAPI @ 0x7f38...] ...`.
_PROBE_TAG: Final = re.compile(r"^\[([A-Za-z0-9_ ]+) @ 0x[0-9a-f]+\]")
# What `hw_device_init_from_type` prints after the component has explained itself.
_PROBE_RESULT: Final = re.compile(r"^Device creation failed: -?\d+\.?$", re.I)


def _devices_requested(encoder: Encoder) -> frozenset[str]:
    """Return the hardware devices this encoder asks for, e.g. `-vaapi_device` for VAAPI.

    What separates noise from evidence. A device the command never mentioned failing to open
    is `-hwaccel auto` shrugging; the one it did mention failing to open is the whole reason
    the conversion is not happening.
    """
    return frozenset(
        argument[1 : -len("_device")].lower()
        for argument in encoder.input_args
        if argument.startswith("-") and argument.endswith("_device")
    )


def _without_hwaccel_probe(detail: str, *, requested: frozenset[str]) -> str:
    """Drop what `-hwaccel auto` said while failing to find a decoder to use."""
    kept: list[str] = []
    dropping = False
    for line in detail.splitlines():
        tag = _PROBE_TAG.match(line)
        if tag is not None:
            component = tag.group(1).strip().lower()
            dropping = component in _PROBE_DEVICES and component not in requested
            if dropping:
                continue
        elif dropping and _PROBE_RESULT.match(line.strip()):
            # The verdict belonging to the line just dropped, which ffmpeg writes untagged.
            continue
        else:
            dropping = False
        kept.append(line)
    return "\n".join(kept).strip()


def _cpu_seconds(pid: int) -> float | None:
    """Processor time this process has used, or None where that cannot be read.

    Linux only, which is every installation Home Assistant supports for the add-on and
    container builds alike. Anywhere else this declines to answer, and the diagnosis simply
    stops short of naming which side is slow rather than guessing wrongly.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command is parenthesised and may itself contain spaces and brackets, so the
        # fields are counted from the last ')': utime and stime are the 12th and 13th.
        fields = stat[stat.rindex(")") + 1 :].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _cpu_load(before: float | None, after: float | None, elapsed: float) -> float | None:
    """Processor cores the converter was using, or None where it could not be measured."""
    if before is None or after is None or elapsed <= 0:
        return None
    return max(0.0, (after - before) / elapsed)


def _diagnose_no_output(
    stream: _Stream,
    *,
    elapsed: float,
    load: float | None,
    opened: bool | None,
    progress: str,
) -> Diagnosis:
    """Say why a stream produced too little to play, as precisely as the evidence allows.

    `opened` is whether ffmpeg got far enough to write a header — which separates a recorder
    that never answered from one that answered and then dribbled. It is None on the MP4
    route, where there is no header to look for.
    """
    if (known := _classify_ffmpeg_error(stream.error_text)) is not None:
        return known

    if stream.process.returncode is not None:
        # Gone, without saying anything recognisable. A hardware encoder that dies this
        # early is much the likeliest cause, and is the one thing here worth not retrying.
        hardware = stream.encoder.hardware
        blamed = (
            f", using this machine's {stream.encoder.name} hardware encoder. Playing the "
            "clip again will use software encoding instead."
            if hardware
            else ". The recorder most likely closed the connection."
        )
        return Diagnosis(
            "stopped_early",
            f"Home Assistant's video converter stopped after {elapsed:.0f} seconds "
            f"without producing anything{blamed}",
            hardware,
        )

    if opened is False:
        return Diagnosis(
            "device_sent_nothing",
            f"The recorder accepted the request but sent no video within {elapsed:.0f} "
            "seconds. This usually means it is busy serving other streams — try again, or "
            "try the other resolution.",
        )

    # Still running, still behind. Which side is holding things up is answerable rather
    # than guessable: a converter using a core is doing work, and one sitting idle is
    # waiting to be sent something.
    if load is not None and load >= _BUSY_LOAD:
        return Diagnosis(
            "machine_too_slow",
            f"This Home Assistant machine cannot convert this recording fast enough to "
            f"play it: {progress} in {elapsed:.0f} seconds, with the converter using "
            f"{load:.1f} processor cores ({stream.encoder.name}). The lower resolution "
            "stream is far cheaper to convert.",
        )
    if load is not None and load <= _IDLE_LOAD:
        return Diagnosis(
            "device_too_slow",
            f"The recorder is sending this recording too slowly to play: {progress} in "
            f"{elapsed:.0f} seconds, while Home Assistant sat idle waiting for it. "
            "Recorders do this when several streams are being read at once.",
        )
    return Diagnosis(
        "too_slow",
        f"This recording could not be prepared in time: {progress} in {elapsed:.0f} "
        "seconds. Trying the other resolution usually helps.",
    )


# ------------------------------------------------------------------------- sessions


class _Stream:
    """A running ffmpeg, and what is needed to stop it or explain why it stopped."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        label: str,
        encoder: Encoder,
        mode: str = MODE_COPY,
    ) -> None:
        """Start draining stderr immediately, so a full pipe cannot stall ffmpeg."""
        self.process = process
        self.label = label
        self.encoder = encoder
        # Carried so a failure can be reported against the rung that produced it: the same
        # message means different things for a remux and for a re-encode.
        self.mode = mode
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
    def error_text(self) -> str:
        """Everything ffmpeg said about this conversion, with the hardware probe dropped.

        What gets classified. All of it, because the line that explains a failure is very
        often not among the first few — see `_without_hwaccel_probe` for how that went.

        Scrubbed of credentials before anything else sees it: ffmpeg repeats the input
        URL in its complaints, and on the NVR route that URL carries the recorder's
        username and password.
        """
        return scrub_credentials(
            _without_hwaccel_probe(
                self._stderr.decode(errors="replace").strip(),
                requested=_devices_requested(self.encoder),
            )
        )

    @property
    def error_detail(self) -> str:
        """What ffmpeg said, trimmed to something worth quoting in a log line."""
        return self.error_text[:_DETAIL_LIMIT]

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
        mode: str = MODE_COPY,
    ) -> None:
        """Start the idle watchdog along with the stream."""
        super().__init__(process, label, encoder, mode)
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
        # Never cancel the task this is running in. The watchdog stops its own session when
        # nobody is reading it — the ordinary way a session ends, since the ordinary way to
        # stop watching is to close the panel — and cancelling itself here delivered a
        # `CancelledError` at the first await below, which is inside `super().async_stop()`.
        # ffmpeg died, because it is killed before that await; the directory was never
        # removed, because that line was never reached. Session directories therefore
        # accumulated in the temporary filesystem, which on most installations is memory,
        # until it filled and every subsequent conversion failed with no space left.
        #
        # A watchdog that got here by deciding to stop is already on its way out, so there
        # is nothing to cancel; one cancelled from anywhere else still needs cancelling.
        if self._watchdog is not asyncio.current_task():
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
        # The last few conversions that produced nothing, newest last. Kept because the
        # people who hit this cannot reproduce it on request and the maintainer cannot
        # reproduce it at all: this is what the panel shows and what diagnostics exports.
        self.failures: deque[dict[str, Any]] = deque(maxlen=_FAILURE_HISTORY)
        self._current: _Stream | None = None
        self._lock = asyncio.Lock()
        # Separate from the slot's lock, and held for much longer: choosing an encoder now
        # means running each candidate, and the slot has to stay claimable while that happens.
        self.encoder_lock = asyncio.Lock()

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

    def note_failure(self, stream: _Stream, diagnosis: Diagnosis, *, mode: str) -> None:
        """Record why a conversion produced nothing, and act on it if it names the encoder.

        The one place that decides what a failure means, so the two views cannot drift: the
        panel is told, the log is written, and only a diagnosis that actually implicates the
        encoder disables it. That last part matters — blaming it for every failure is how a
        slow recorder ends up permanently costing a working GPU.
        """
        self.failures.append(
            {
                **diagnosis.as_dict(),
                "label": stream.label,
                "mode": mode,
                "encoder": stream.encoder.name if mode == MODE_ENCODE else "copy",
                "ffmpeg": stream.error_detail or "",
                "at": dt_util.utcnow().isoformat(),
            }
        )
        _LOGGER.warning(
            "Restreaming %s produced nothing (%s): %s [ffmpeg said: %s]",
            stream.label,
            diagnosis.code,
            diagnosis.message,
            stream.error_detail or "nothing",
        )
        if diagnosis.encoder_at_fault:
            self.note_encoder_failure(stream.encoder)

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


def _sweep_sessions() -> int:
    """Delete session directories belonging to runs that are over, and count them.

    Bounded by age rather than by ownership, because this also runs on a reload and a
    reload does not stop a session that is playing. Nothing legitimate can be older than
    `HLS_MAX_SESSION_SECONDS` — the watchdog stops a session at that age however diligently
    it is being read — and a live session's directory is touched continuously as segments
    are written and rotated out, so its modification time is always recent.
    """
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - HLS_MAX_SESSION_SECONDS
    try:
        candidates = list(root.glob(f"{SESSION_PREFIX}*"))
    except OSError:
        return 0

    removed = 0
    for directory in candidates:
        try:
            if not directory.is_dir() or directory.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
    return removed


async def async_sweep_sessions(hass: HomeAssistant) -> int:
    """Reclaim what earlier runs left behind, and say how much was found.

    Each session removes its own directory as it ends, so in a healthy installation this
    finds nothing. It exists because that teardown was once skipped whenever the idle
    watchdog was the one stopping the session — which is to say whenever a viewer simply
    closed the panel, the ordinary case — and the directories left over from it are not
    reclaimed by restarting Home Assistant. Depending on how the temporary filesystem is
    mounted they survive until the machine reboots, or indefinitely.

    Swept at setup rather than on a timer: what accumulated did so under a version that is
    no longer running, and one pass gets it back.
    """
    removed = await hass.async_add_executor_job(_sweep_sessions)
    if removed:
        _LOGGER.info(
            "Reolink Stamina removed %s playback session director%s left behind by an earlier run",
            removed,
            "y" if removed == 1 else "ies",
        )
    return removed


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
        await hass.async_add_executor_job(lambda: tempfile.mkdtemp(prefix=SESSION_PREFIX))
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

    session = _HlsStream(hass, process, label, encoder, token, directory, mode)
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

        active = _Stream(process, label, encoder, mode)
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
