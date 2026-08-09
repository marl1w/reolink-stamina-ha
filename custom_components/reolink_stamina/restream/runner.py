"""Starting a conversion, and the two ways that can refuse."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import secrets
import shutil
import tempfile

from homeassistant.core import HomeAssistant

from ..ffmpeg import async_ffmpeg_binary
from ..playback_route import (
    Recording,
    async_playback_secrets,
    async_playback_source,
)
from ..tls import async_verify_tls
from .common import (
    FORMAT_HLS,
    FORMAT_MP4,
    MODE_COPY,
    MODE_ENCODE,
    SESSION_PREFIX,
    SOFTWARE_ENCODER,
    Encoder,
)
from .encoders import (
    async_choose_encoder,
    build_args,
)
from .sessions import (
    _HlsStream,
    async_get_manager,
)

_LOGGER = logging.getLogger(__name__)


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
        verify_tls=async_verify_tls(hass),
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
    recording = Recording(
        entry_id=entry_id,
        channel=channel,
        stream=stream,
        filename=filename,
        start_id=start_id,
        playback_id=playback_id,
        seek=max(0, int(seek)),
    )
    source = await async_playback_source(hass, recording)
    credentials = async_playback_secrets(hass, entry_id)
    token = secrets.token_urlsafe(24)
    label = f"{recording.label} hls"
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

    session = _HlsStream(hass, process, label, encoder, token, directory, mode, credentials)
    await async_get_manager(hass).async_claim(session)
    return token
