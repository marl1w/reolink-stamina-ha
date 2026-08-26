"""WebDAV, through the client its own integration already holds.

The one most NAS boxes answer without being taught anything: Synology, QNAP, TrueNAS and
OpenMediaVault all speak it, as does Nextcloud. The WebDAV integration keeps a connected
client on its config entry, so there is no second address, login or certificate to enter
here — if it works for backups, it works for clips.

Nothing here imports `aiowebdav2` at module level. The library is installed by Home
Assistant when that integration is set up, which is the only situation in which this class
is ever constructed, but importing it up here would make the whole destinations package
unimportable for everyone else.
"""

from __future__ import annotations

from collections.abc import Awaitable
from io import BytesIO
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .base import Destination, DestinationError, TransientError, async_attempt

_LOGGER = logging.getLogger(__name__)


def _size(raw: object) -> int | None:
    """Return one WebDAV size as a number, or None when the server did not give one."""
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else None


def _classify(what: str, err: Exception) -> Exception:
    """Decide whether one `aiowebdav2` failure is worth another try.

    A connection that dropped or a server that is briefly busy will very likely answer the
    next request; a rejected path or a refused login will not, however many times it is
    asked. The import is done here rather than at module level, and by the time a call has
    failed the library is certainly loaded — the client that failed came from it.
    """
    from aiowebdav2 import exceptions as webdav

    permanent = (
        webdav.NotValidError,
        webdav.NotFoundError,
        webdav.MethodNotSupportedError,
        webdav.NotEnoughSpaceError,
        webdav.UnauthorizedError,
        webdav.AccessDeniedError,
    )
    if isinstance(err, permanent):
        return DestinationError(f"WebDAV refused the {what}: {err}")
    return TransientError(f"{type(err).__name__}: {err}")


class WebDavDestination(Destination):
    """Clips on a WebDAV server, under the folder the syncer was configured with."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded WebDAV config entry."""
        self._hass = hass
        self._entry = entry

    @property
    def label(self) -> str:
        """Return the server this uploads to."""
        return f"WebDAV ({self._entry.title})"

    @property
    def _client(self) -> Any:
        """Return the connected client the WebDAV integration keeps on its entry."""
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")
        client = getattr(self._entry, "runtime_data", None)
        if client is None:
            raise DestinationError(f"{self._entry.title} has no WebDAV client")
        return client

    async def _async_call[T](self, what: str, call: Awaitable[T]) -> T:
        """Await one client call, classifying whatever it raises."""
        try:
            return await call
        except DestinationError:
            raise
        except Exception as err:
            raise _classify(what, err) from err

    async def _async_ensure_folder(self, folder: str) -> None:
        """Create the folder and its parents, which WebDAV will not do on upload."""
        client = self._client
        parts = [part for part in folder.split("/") if part]
        for depth in range(1, len(parts) + 1):
            branch = "/".join(parts[:depth])
            if await self._async_call("folder check", client.check(branch)):
                continue
            if not await self._async_call("folder creation", client.mkdir(branch)):
                raise DestinationError(f"WebDAV would not create the folder {branch}")

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one clip, and confirm from the server's own answer that it landed.

        The size is read back rather than assumed: a truncated upload the server accepted is
        worse than a reported failure, because a failure gets retried and a short file is
        quietly kept.
        """
        folder, _, _ = path.rpartition("/")

        async def attempt() -> None:
            client = self._client
            if folder:
                await self._async_ensure_folder(folder)
            # A fresh buffer per attempt: a retry cannot re-read a consumed one.
            await self._async_call(
                f"upload of {path}",
                client.upload_iter(BytesIO(data), path, content_length=len(data)),
            )
            info = await self._async_call(f"check of {path}", client.info(path))
            # Every value comes back as a string, and an absent `getcontentlength` is an
            # empty one rather than a missing key — so a server that does not report a size
            # must read as "cannot tell", not as zero bytes stored.
            stored = _size(info.get("size"))
            if stored is not None and stored != len(data):
                raise DestinationError(
                    f"WebDAV stored {stored} bytes of {path}, not the {len(data)} sent"
                )

        await async_attempt(f"WebDAV upload of {path}", attempt)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def async_delete(self, path: str) -> None:
        """Remove one clip, treating an already-missing file as success."""

        async def attempt() -> None:
            client = self._client
            if not await self._async_call(f"check of {path}", client.check(path)):
                return
            await self._async_call(f"delete of {path}", client.clean(path))

        await async_attempt(f"WebDAV delete of {path}", attempt)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""

        async def attempt() -> dict[str, int]:
            client = self._client
            if not await self._async_call(f"check of {folder}", client.check(folder)):
                return {}  # the folder does not exist yet, which is not an error
            entries = await self._async_call(f"listing of {folder}", client.list_with_infos(folder))
            found: dict[str, int] = {}
            for entry in entries:
                # `isdir` arrives as the string form of a bool.
                if str(entry.get("isdir", "")).lower() == "true":
                    continue
                # The name is taken from the entry's own path rather than its
                # `displayname`, which servers are free to omit — and an omitted one would
                # drop every clip from this listing, so the index would forget the lot and
                # the quota would never evict anything again.
                name = str(entry.get("path", "")).rstrip("/").rpartition("/")[2]
                if name:
                    found[f"{folder}/{name}"] = _size(entry.get("size")) or 0
            return found

        return await async_attempt(f"WebDAV listing of {folder}", attempt)
