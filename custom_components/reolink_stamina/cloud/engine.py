"""One syncer per recorder: detections in, clips in the cloud out.

The parts it leans on are all tested separately — `windows` decides what counts as one clip,
`index` decides what has to go to make room, `naming` decides what it is called, `fetch`
decides how to get the bytes and `destination` puts them somewhere. What is left here is the
orchestration, and the two rules that keep it from hurting anything:

* **One fetch at a time per recorder.** A person walking past sets off three cameras, so an
  event routinely produces several clips on one NVR. They queue.
* **The switch gates admission, not delivery**, and admission is decided at the *first
  detection* — not when the clip is finally ready some forty seconds later. Turning it off
  stops new events being taken on; an event already under way still becomes a clip, and
  anything queued still uploads. Disarming an alarm should not discard footage of the event
  that made you disarm it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import datetime as dt
import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from ..const import (
    DOMAIN,
    STORAGE_VERSION,
    SYNC_MAX_CLIP_BYTES,
    SYNC_MAX_WINDOW_SECONDS,
    SYNC_SETTLE_SECONDS,
    SYNC_WRITE_DELAY_SECONDS,
)
from ..detections import async_detection_entities
from ..flv_proxy import async_playback_source
from ..nvr_registry import async_discover_nvrs, async_get_host
from .destination import Destination, DestinationError
from .fetch import (
    FetchError,
    FfmpegMissingError,
    async_cut_with_ffmpeg,
    async_ffmpeg_binary,
    async_read_stream,
    stable,
    wants_whole_file,
)
from .index import ClipIndex, StoredClip
from .naming import clip_filename, remote_path
from .windows import ClipWindow, WindowCollector

_LOGGER = logging.getLogger(__name__)

# How often finished windows are looked for. Detections arrive by push; this only decides how
# promptly a *finished* event is noticed, so a few seconds is plenty.
TICK = dt.timedelta(seconds=5)

# How long to keep asking whether a growing recording has finished growing.
STABLE_ATTEMPTS = 12
STABLE_INTERVAL = 10.0

# A clip that fails is retried, but not for ever: a recorder that has already overwritten the
# footage will never produce it.
MAX_ATTEMPTS = 3

# How often the set of watched sensors is rebuilt. It is not fixed for the life of the entry: a
# recorder that was unreachable when Home Assistant started exposes no detection sensors to
# find, cameras get added to an NVR, and a Reolink config entry can be reloaded under us.
RESOLVE_INTERVAL = dt.timedelta(minutes=5)

# One fetch at a time per recorder, shared by every syncer in the instance.
_NVR_LOCKS: dict[str, asyncio.Lock] = {}


def _nvr_lock(entry_id: str) -> asyncio.Lock:
    """Return the lock serialising work against one recorder."""
    return _NVR_LOCKS.setdefault(entry_id, asyncio.Lock())


@dataclass(slots=True, frozen=True)
class WatchedSensor:
    """One detection sensor being watched, and what it belongs to."""

    entry_id: str
    channel: int
    nvr: str
    camera: str
    # The panel's own vocabulary, as resolved from the sensor's unique id — not the entity id's
    # suffix, which is whatever Reolink happened to call it. `pet` and `animal` are the same
    # thing to us, and one recorder says each.
    kind: str

    @property
    def key(self) -> str:
        """Return the per-camera key that windows are gathered under."""
        return f"{self.entry_id}|{self.channel}"


@dataclass(slots=True)
class SyncJob:
    """One clip waiting to be fetched and uploaded."""

    entry_id: str
    channel: int
    nvr: str
    camera: str
    window: ClipWindow
    attempts: int = 0


@dataclass(slots=True)
class SyncStatus:
    """What the sensors report."""

    quota: int = 0
    used: int = 0
    clips: int = 0
    queued: int = 0
    uploaded: int = 0
    last_upload: dt.datetime | None = None
    last_error: str | None = None
    pending_windows: int = 0

    @property
    def free(self) -> int:
        """Return the bytes still available inside the quota."""
        return max(0, self.quota - self.used)


class NvrSyncer:
    """Watches one recorder's cameras and keeps their clips in the cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        *,
        nvr_name: str,
        entry_id: str,
        destination: Destination,
        kinds: set[str],
        quota: int,
        folder: str,
        stream: str,
        lead: float,
        tail: float,
        nvr_device: tuple[str, str] | None = None,
        destination_device: str | None = None,
    ) -> None:
        """Configure a syncer; nothing is watched until `async_start`."""
        self.hass = hass
        self.entry = entry
        self.subentry = subentry
        self.nvr_name = nvr_name
        self.entry_id = entry_id
        self.destination = destination
        self.kinds = kinds
        self.quota = quota
        self.folder = folder
        self.stream = stream

        self.accepting = True
        self.status = SyncStatus(quota=quota)
        # Shown on the sync device so it is obvious where its clips come from.
        self.nvr_device = nvr_device
        self.destination_device = destination_device
        self.camera_names: list[str] = []

        self._windows = WindowCollector(
            lead=lead,
            tail=tail,
            settle=SYNC_SETTLE_SECONDS,
            maximum=SYNC_MAX_WINDOW_SECONDS,
        )
        self._index = ClipIndex()
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.sync.{subentry.subentry_id}", private=True
        )
        self._queue: deque[SyncJob] = deque()
        self._sensors: dict[str, WatchedSensor] = {}
        # Held apart from `_unsubscribe` because it is replaced whenever the sensor set changes.
        self._unwatch = None
        self._unsubscribe: list = []
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._listeners: list = []

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load state, reconcile with the destination, and begin watching."""
        stored = await self._store.async_load() or {}
        self._index = ClipIndex.from_list(stored.get("clips"))
        self.accepting = bool(stored.get("accepting", True))

        await self._async_reconcile()
        self._async_watch_sensors()

        self._unsubscribe.append(async_track_time_interval(self.hass, self._async_tick, TICK))
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._async_watch_sensors, RESOLVE_INTERVAL)
        )
        self._worker = self.hass.async_create_background_task(
            self._async_work(), f"{DOMAIN} sync {self.subentry.subentry_id}"
        )
        self._publish()
        _LOGGER.debug(
            "Cloud sync for %s watching %s sensors, %s clips held",
            self.nvr_name,
            len(self._sensors),
            len(self._index),
        )

    async def async_stop(self) -> None:
        """Stop watching, keeping whatever was gathered."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        # Anything mid-event is queued rather than dropped, then persisted for next time.
        for window in self._windows.flush(dt.datetime.now()):
            self._enqueue(window)
        if self._worker:
            self._worker.cancel()
            self._worker = None
        await self._async_save()

    def async_add_listener(self, listener) -> callable:
        """Register an entity to be told when the status changes."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _publish(self) -> None:
        """Refresh the status and tell the entities."""
        self.status.used = self._index.used
        self.status.clips = len(self._index)
        self.status.queued = len(self._queue)
        self.status.pending_windows = self._windows.pending
        for listener in list(self._listeners):
            listener()

    async def _async_save(self) -> None:
        """Persist the index and the switch, so a restart resumes where it left off."""
        self._store.async_delay_save(
            lambda: {"clips": self._index.as_list(), "accepting": self.accepting}, 5
        )

    async def async_set_accepting(self, accepting: bool) -> None:
        """Turn admission of new clips on or off."""
        self.accepting = accepting
        await self._async_save()
        self._publish()

    # -------------------------------------------------------------------- watching

    @callback
    def _async_watch_sensors(self, _now: dt.datetime | None = None) -> None:
        """Resolve the sensors to watch and subscribe to them, replacing any earlier set.

        Repeated on `RESOLVE_INTERVAL` rather than done once at startup, because resolution can
        legitimately come up empty: a recorder that is unreachable is skipped, and it has no
        detection sensors in the registry to find anyway. Nothing else would ever look again, so
        a syncer whose NVR was down when Home Assistant started sat there watching nothing —
        no windows, no clips, and no error to say why — until the integration was reloaded.
        """
        previous = set(self._sensors)
        self._resolve_sensors()
        if set(self._sensors) == previous:
            return

        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        if self._sensors:
            self._unwatch = async_track_state_change_event(
                self.hass, list(self._sensors), self._async_state_changed
            )
        _LOGGER.debug(
            "Cloud sync for %s now watching %s sensors (was %s)",
            self.nvr_name,
            len(self._sensors),
            len(previous),
        )
        # The switch reports the cameras it found, so it has to refresh too.
        self._publish()

    def _resolve_sensors(self) -> None:
        """Map every detection sensor of every camera on this syncer's recorder."""
        self._sensors = {}
        self.camera_names = []
        nvr = next(
            (
                item
                for item in async_discover_nvrs(self.hass)
                if item.entry_id == self.entry_id and item.status == "ok"
            ),
            None,
        )
        if nvr is None:
            return
        # The recorder's own name, refreshed here rather than kept from setup: renaming the
        # NVR should follow through to the clips without a restart.
        self.nvr_name = nvr.name
        for camera in nvr.cameras:
            self.camera_names.append(camera.name)
            found = async_detection_entities(self.hass, nvr.entry_id, camera.channel)
            for entity_id, kind in found.items():
                if kind in self.kinds:
                    self._sensors[entity_id] = WatchedSensor(
                        entry_id=nvr.entry_id,
                        channel=camera.channel,
                        nvr=nvr.name,
                        camera=camera.name,
                        kind=kind,
                    )

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Fold a detection sensor's change into its camera's window."""
        entity_id = event.data["entity_id"]
        sensor = self._sensors.get(entity_id)
        if sensor is None:
            return
        key = sensor.key
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None:
            return
        moment = dt.datetime.now()
        if new.state == "on" and (old is None or old.state != "on"):
            # This is the one place the switch is consulted: admission happens as the event
            # opens, and is never revisited. While the switch is off nothing is gathered at
            # all, so a recorder that is not syncing costs nothing to watch.
            taken = self._windows.record_on(
                key,
                entity_id,
                sensor.kind,
                moment,
                accepting=self.accepting,
            )
            if not taken:
                _LOGGER.debug(
                    "Cloud sync for %s is not accepting; ignored %s",
                    self.nvr_name,
                    entity_id,
                )
                return
            self._publish()
        elif new.state != "on" and old is not None and old.state == "on":
            self._windows.record_off(key, entity_id, moment)
            self._publish()

    @callback
    def _async_tick(self, _now) -> None:
        """Hand over any window that has finished."""
        for window in self._windows.collect(dt.datetime.now()):
            self._enqueue(window)

    @callback
    def _enqueue(self, window: ClipWindow) -> None:
        """Queue a finished window.

        The switch is deliberately not consulted here. A window only exists because it was
        accepted when it opened, and admission is not revisited — see
        `WindowCollector.record_on`. Re-reading it at this point discarded exactly the footage
        the switch is meant to preserve: you arrive, the camera sees you, you disarm, and the
        clip settles twenty seconds later to find the switch already off.
        """
        sensor = next(
            (value for value in self._sensors.values() if value.key == window.key),
            None,
        )
        if sensor is None:
            return
        self._queue.append(
            SyncJob(
                entry_id=sensor.entry_id,
                channel=sensor.channel,
                nvr=sensor.nvr,
                camera=sensor.camera,
                window=window,
            )
        )
        self._wake.set()
        self._publish()

    # --------------------------------------------------------------------- working

    async def _async_work(self) -> None:
        """Process the queue, one clip at a time."""
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = self._queue.popleft()
            self._publish()
            try:
                await self._async_handle(job)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                job.attempts += 1
                self.status.last_error = str(err)
                _LOGGER.warning(
                    "Cloud sync for %s could not upload %s: %s", self.nvr_name, job.camera, err
                )
                if job.attempts < MAX_ATTEMPTS:
                    self._queue.append(job)
                self._publish()

    async def _async_handle(self, job: SyncJob) -> None:
        """Fetch one clip and put it in the cloud."""
        # The recorder needs a moment before a new recording is findable at all.
        delay = (
            job.window.end + dt.timedelta(seconds=SYNC_WRITE_DELAY_SECONDS) - dt.datetime.now()
        ).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        async with _nvr_lock(job.entry_id):
            recording = await self._async_settled_recording(job)
            if recording is None:
                raise FetchError("no recording covers this event")
            data = await self._async_bytes(job, recording)

        name = clip_filename(job.window.start, job.nvr, job.camera)
        path = remote_path(self.folder, name)

        for doomed in self._index.plan_eviction(len(data), self.quota):
            await self.destination.async_delete(doomed.path)
            self._index.remove(doomed.path)
            _LOGGER.debug("Evicted %s to make room", doomed.path)

        if self._index.used + len(data) > self.quota:
            raise DestinationError(f"{len(data)} bytes cannot fit in a {self.quota} byte quota")

        await self.destination.async_store(path, data)
        self._index.add(StoredClip(path=path, size=len(data), recorded=job.window.start))
        self.status.uploaded += 1
        self.status.last_upload = dt.datetime.now()
        self.status.last_error = None
        await self._async_save()
        self._publish()
        _LOGGER.info(
            "Cloud sync uploaded %s (%.1f MB, %.0fs) to %s",
            path,
            len(data) / 1e6,
            job.window.seconds,
            self.destination.label,
        )

    async def _async_settled_recording(self, job: SyncJob) -> dict | None:
        """Find the recording covering this window, once it has stopped growing.

        Searched per day, so an event running over midnight has to ask for both: the footage of
        someone arriving at 23:59:55 is written to the new day, and looking only in the day the
        window started in found nothing at all.
        """
        from ..vod import async_search_day

        # Ordered, de-duplicated: one day in the ordinary case, two across a midnight.
        dates = list(dict.fromkeys([job.window.start.date(), job.window.end.date()]))

        previous_end: dt.datetime | None = None
        for _attempt in range(STABLE_ATTEMPTS):
            files: list[dict] = []
            for date in dates:
                found, _unlabelled = await async_search_day(
                    self.hass,
                    job.entry_id,
                    job.channel,
                    self.stream,
                    date,
                    0,  # unsplit: the recorder's own files are what we want to reason about
                    # This search only locates the bytes for a window that was already
                    # decided from the sensors, so classifying it again would be a history
                    # query per attempt, twelve times per clip, for an answer we have.
                    classify=False,
                )
                files.extend(found)
            covering = [
                item
                for item in files
                if dt.datetime.fromisoformat(item["end"]).replace(tzinfo=None) > job.window.start
                and dt.datetime.fromisoformat(item["start"]).replace(tzinfo=None) < job.window.end
            ]
            if covering:
                newest = max(covering, key=lambda item: item["end"])
                end = dt.datetime.fromisoformat(newest["end"]).replace(tzinfo=None)
                if stable(previous_end, end) or end >= job.window.end:
                    return newest
                previous_end = end
            await asyncio.sleep(STABLE_INTERVAL)
        return None

    async def _async_bytes(self, job: SyncJob, recording: dict) -> bytes:
        """Get the clip's bytes, whole or cut, whichever suits the recording."""
        session = async_get_clientsession(self.hass)
        duration = float(recording.get("duration") or 0.0)
        wanted = job.window.seconds

        if wants_whole_file(duration, wanted):
            # The recorder already made the clip; take it as it is, at wire speed.
            from reolink_aio.enums import VodRequestType

            host = async_get_host(self.hass, job.entry_id)
            _mime, url = await host.api.get_vod_source(
                job.channel, recording["name"], self.stream, VodRequestType.DOWNLOAD
            )
            async with session.get(url) as response:
                if response.status != 200:
                    raise FetchError(f"the recorder answered HTTP {response.status}")
                return await async_read_stream(response, SYNC_MAX_CLIP_BYTES)

        binary = async_ffmpeg_binary(self.hass)
        if binary is None:
            raise FfmpegMissingError(
                "this camera records continuously, so the clip has to be cut, and no ffmpeg "
                "is installed"
            )
        # Only the clip's bytes cross the network: playback is seeked server-side.
        file_start = dt.datetime.fromisoformat(recording["start"]).replace(tzinfo=None)
        seek = max(0.0, (job.window.start - file_start).total_seconds())
        source = await async_playback_source(
            self.hass,
            job.entry_id,
            job.channel,
            self.stream,
            recording["name"],
            recording.get("file_start_id") or recording["start_id"],
            recording.get("playback_id", ""),
            int(seek + float(recording.get("offset") or 0.0)),
        )
        return await async_cut_with_ffmpeg(binary, source, wanted, SYNC_MAX_CLIP_BYTES)

    async def _async_reconcile(self) -> None:
        """Forget clips the destination no longer holds, so the quota reflects reality."""
        folder = remote_path(self.folder, "").rstrip("/")
        try:
            present = await self.destination.async_list(folder)
        except DestinationError as err:
            self.status.last_error = str(err)
            return
        forgotten = self._index.reconcile(set(present))
        if forgotten:
            _LOGGER.debug(
                "Cloud sync for %s forgot %s clips no longer in the cloud",
                self.nvr_name,
                len(forgotten),
            )
            await self._async_save()
