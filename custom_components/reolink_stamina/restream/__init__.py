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

Split from a single 1,400-line module. The pieces are separate concerns — what this machine
can encode, what is running, why one failed, and where the results are served — and they
share only the constants. The dependency order is common, diagnosis, sessions, encoders,
runner, views, and it is deliberately a line rather than a web.

Everything the rest of the integration uses is re-exported here, so it stays one import.
"""

from __future__ import annotations

from .common import (
    FIRST_OUTPUT_TIMEOUT,
    FORMAT_HLS,
    FORMAT_MP4,
    HLS_IDLE_TIMEOUT,
    HLS_INIT,
    HLS_MAX_SESSION_SECONDS,
    HLS_PLAYLIST,
    MAX_HEIGHT,
    MODE_COPY,
    MODE_ENCODE,
    RESTREAM_FORMATS,
    RESTREAM_MODES,
    RESTREAM_PREFIX,
    SESSION_PREFIX,
    SOFTWARE_ENCODER,
    Encoder,
    async_hls_path,
    async_restream_path,
)

# The private names are re-exported deliberately. The split is meant to be invisible from
# outside, and the tests reach for these on purpose — they are the parts most worth pinning.
from .diagnosis import (
    Diagnosis,
    _classify_ffmpeg_error,
    _cpu_load,
    _cpu_seconds,
    _devices_requested,
    _diagnose_no_output,
    _without_hwaccel_probe,
)
from .encoders import _available_encoders, async_choose_encoder, build_args
from .runner import (
    FfmpegUnavailableError,
    RestreamError,
    async_beta_enabled,
    async_start_hls,
)
from .sessions import (
    RestreamManager,
    _HlsStream,
    _Stream,
    async_get_manager,
    async_shutdown,
    async_sweep_sessions,
)
from .views import ReolinkStaminaHlsView, ReolinkStaminaRestreamView

__all__ = [
    "FIRST_OUTPUT_TIMEOUT",
    "FORMAT_HLS",
    "FORMAT_MP4",
    "HLS_IDLE_TIMEOUT",
    "HLS_INIT",
    "HLS_MAX_SESSION_SECONDS",
    "HLS_PLAYLIST",
    "MAX_HEIGHT",
    "MODE_COPY",
    "MODE_ENCODE",
    "RESTREAM_FORMATS",
    "RESTREAM_MODES",
    "RESTREAM_PREFIX",
    "SESSION_PREFIX",
    "SOFTWARE_ENCODER",
    "Diagnosis",
    "Encoder",
    "FfmpegUnavailableError",
    "ReolinkStaminaHlsView",
    "ReolinkStaminaRestreamView",
    "RestreamError",
    "RestreamManager",
    "_HlsStream",
    "_Stream",
    "_available_encoders",
    "_classify_ffmpeg_error",
    "_cpu_load",
    "_cpu_seconds",
    "_devices_requested",
    "_diagnose_no_output",
    "_without_hwaccel_probe",
    "async_beta_enabled",
    "async_choose_encoder",
    "async_get_manager",
    "async_hls_path",
    "async_restream_path",
    "async_shutdown",
    "async_start_hls",
    "async_sweep_sessions",
    "build_args",
]
