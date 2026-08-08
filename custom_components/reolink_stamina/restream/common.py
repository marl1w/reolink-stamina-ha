"""The constants every part of this shares, the encoder record, and the URLs."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Final

from ..const import DOMAIN
from ..playback_route import (
    Recording,
)

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

RESTREAM_PREFIX: Final = "/api/reolink_stamina/restream"


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
    return Recording(
        entry_id=entry_id,
        channel=channel,
        stream=stream,
        filename=filename,
        start_id=start_id,
        playback_id=playback_id,
        seek=max(0, int(seek)),
    ).path(f"{RESTREAM_PREFIX}/{mode}")


def async_hls_path(token: str) -> str:
    """Return the path of a live HLS session's playlist."""
    return f"/api/reolink_stamina/hls/{token}/{HLS_PLAYLIST}"
