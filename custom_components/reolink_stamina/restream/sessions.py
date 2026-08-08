"""The conversions currently running, and clearing up after the ones that are not.

A session owns a process and a temporary directory, and both outlive the request that created
them. Nothing here decides *whether* to convert; it only keeps track of what is.
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..redact import scrub_credentials
from .common import (
    _DETAIL_LIMIT,
    _FAILURE_HISTORY,
    _HLS_SWEEP_INTERVAL,
    _MANAGER_KEY,
    _STDERR_LIMIT,
    HLS_IDLE_TIMEOUT,
    HLS_MAX_SESSION_SECONDS,
    MODE_COPY,
    MODE_ENCODE,
    SESSION_PREFIX,
    Encoder,
)
from .diagnosis import (
    Diagnosis,
    _devices_requested,
    _without_hwaccel_probe,
)

_LOGGER = logging.getLogger(__name__)


class _Stream:
    """A running ffmpeg, and what is needed to stop it or explain why it stopped."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        label: str,
        encoder: Encoder,
        mode: str = MODE_COPY,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Start draining stderr immediately, so a full pipe cannot stall ffmpeg."""
        self.process = process
        self.label = label
        self.encoder = encoder
        # Carried so a failure can be reported against the rung that produced it: the same
        # message means different things for a remux and for a re-encode.
        self.mode = mode
        # The credential values in the URL this ffmpeg was given, so its output can be
        # scrubbed exactly long after that URL has gone out of scope.
        self._secrets = secrets
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

        Scrubbed before anything else sees it: ffmpeg repeats the input URL in its
        complaints, and on the `/flv` route that URL carries the recorder's own username
        and password. `secrets` are the literal values, captured when the stream was
        started, so a password the pattern would have to guess the extent of is still
        removed exactly.
        """
        return scrub_credentials(
            _without_hwaccel_probe(
                self._stderr.decode(errors="replace").strip(),
                requested=_devices_requested(self.encoder),
            ),
            secrets=self._secrets,
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
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Start the idle watchdog along with the stream."""
        super().__init__(process, label, encoder, mode, secrets)
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
