"""Turning an ffmpeg failure into something a person can act on.

A wall of stderr tells almost nobody anything. What matters is which of a handful of things
went wrong — no hardware, a codec this build cannot read, a stream that never arrived — and
what can be done about each.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Final

from .common import (
    Encoder,
)

if TYPE_CHECKING:
    # For the annotation only. Importing `sessions` for real would make the two modules
    # import each other, and `from __future__ import annotations` already makes this a string.
    from .sessions import _Stream

_LOGGER = logging.getLogger(__name__)

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
