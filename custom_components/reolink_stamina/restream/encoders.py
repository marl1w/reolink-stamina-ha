"""What this machine can actually encode with, and the arguments that drive it.

Probed once and remembered. Asking ffmpeg what it lists is cheap; finding out that a named
encoder exists but cannot open the device is not, so both questions get asked up front.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import re
from typing import Final

from homeassistant.core import HomeAssistant

from ..tls import ffmpeg_tls_args
from .common import (
    _DETAIL_LIMIT,
    _HARDWARE_ENCODERS,
    _NEEDS_DRI,
    FORMAT_HLS,
    HLS_INIT,
    HLS_PLAYLIST,
    HLS_SEGMENT_SECONDS,
    MAX_HEIGHT,
    MODE_ENCODE,
    SOFTWARE_ENCODER,
    Encoder,
)
from .sessions import (
    async_get_manager,
)

_LOGGER = logging.getLogger(__name__)


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
    verify_tls: bool = False,
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

    # The recorder's FLV carries no usable timestamps at the start of a seek. `verify_tls`
    # follows the option, so ffmpeg agrees with the rest of the integration about the
    # recorder's certificate; it adds nothing in the default case, which ffmpeg already is.
    args += [
        "-fflags",
        "+genpts",
        *ffmpeg_tls_args(source_url, verify_tls=verify_tls),
        "-i",
        source_url,
    ]

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
