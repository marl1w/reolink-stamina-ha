"""Where clips go, and how they get there.

Deliberately a small interface: a clip is bytes with a path, and a destination can store
one, delete one, and say what it already holds. Everything else — quotas, eviction, naming,
deciding which events become clips — is the syncer's business, so adding a second cloud means
implementing three methods rather than understanding the whole feature.

Getting a clip *there* is this module's problem, though, and it is treated as one: the bytes
cost a real-time playback from the recorder to obtain, so a throttled or briefly unavailable
service is waited out and retried here rather than thrown back for the whole clip to be
fetched again. `async_store` returns only once the service has confirmed what it stored.

The first implementation rides on Home Assistant's own OneDrive integration. It borrows that
config entry's OAuth session rather than asking for its own, which is why connecting a cloud
here needs no new credentials, no application registration and no second consent screen: if
OneDrive already works for backups, it works for clips. The cost of that shortcut is the
scope the integration holds — `Files.ReadWrite.AppFolder` — so clips live inside Home
Assistant's own app folder rather than at the root of the drive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Microsoft Graph. The app folder is addressed by name, so nothing here needs the drive id.
GRAPH = "https://graph.microsoft.com/v1.0"
APPROOT = f"{GRAPH}/me/drive/special/approot"

# Graph accepts a single PUT up to 250 MB; clips are measured in single-digit megabytes, so
# the resumable session protocol would be ceremony for nothing.
SIMPLE_UPLOAD_LIMIT = 240 * 1024 * 1024

# Failures worth trying again rather than reporting: Graph throttles under load, and a
# service of that size produces gateway errors that mean nothing about the request.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# A clip's bytes were expensive to get — reading them cost the recorder a real-time
# playback — so an upload that can succeed on a second attempt should not cost that again.
REQUEST_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0


class _Transient(Exception):
    """Internal: a failure worth retrying, with Graph's own wait if it gave one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record what failed and how long to wait."""
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Read Graph's `Retry-After`, which is what a throttled caller is meant to obey."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), MAX_BACKOFF_SECONDS)
    except ValueError:
        # The header may be an HTTP date; the backoff schedule is a fine substitute.
        return None


class DestinationError(Exception):
    """Raised when a destination cannot do what was asked of it."""


class UploadTooLargeError(DestinationError):
    """Raised when a clip exceeds what this destination will take in one go."""


class Destination(ABC):
    """Somewhere clips can be kept."""

    @property
    @abstractmethod
    def label(self) -> str:
        """Return a short human-readable name, for logs and entity attributes."""

    @abstractmethod
    async def async_store(self, path: str, data: bytes) -> None:
        """Write one clip, creating any folders it needs."""

    @abstractmethod
    async def async_delete(self, path: str) -> None:
        """Remove one clip. Succeeds quietly when it is already gone."""

    @abstractmethod
    async def async_list(self, folder: str) -> dict[str, int]:
        """Return `path -> size` for the clips under one folder.

        Used to reconcile the syncer's index at startup, so files deleted by hand stop
        counting against the quota.
        """


class OneDriveDestination(Destination):
    """OneDrive, through the OAuth session its own integration already holds."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded OneDrive config entry."""
        self._hass = hass
        self._entry = entry
        self._session: config_entry_oauth2_flow.OAuth2Session | None = None

    @property
    def label(self) -> str:
        """Return the OneDrive account this uploads to."""
        return f"OneDrive ({self._entry.title})"

    async def _async_token(self) -> str:
        """Return a valid access token, refreshing it the way its owner would.

        The refresh is done through Home Assistant's own session helper, so the token this
        integration uses and the one the OneDrive integration uses stay the same — and a
        revoked login surfaces as that integration's reauth flow rather than as a mystery
        here.
        """
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")

        if self._session is None:
            try:
                implementation = (
                    await config_entry_oauth2_flow.async_get_config_entry_implementation(
                        self._hass, self._entry
                    )
                )
            except ValueError as err:
                raise DestinationError(
                    f"{self._entry.title} has no usable credentials: {err}"
                ) from err
            self._session = config_entry_oauth2_flow.OAuth2Session(
                self._hass, self._entry, implementation
            )

        await self._session.async_ensure_token_valid()
        token = self._session.token.get("access_token")
        if not token:
            raise DestinationError(f"{self._entry.title} returned no access token")
        return str(token)

    async def _async_once(
        self,
        method: str,
        url: str,
        *,
        allow_missing: bool,
        data: bytes | None,
        headers: dict[str, str] | None,
    ) -> Any:
        """Make one Graph call, classifying its failure as permanent or worth retrying."""
        token = await self._async_token()
        session = async_get_clientsession(self._hass)
        sent = {"Authorization": f"Bearer {token}", **(headers or {})}
        try:
            async with session.request(method, url, headers=sent, data=data) as response:
                if response.status == 404:
                    if allow_missing:
                        return None
                    raise DestinationError(f"OneDrive answered HTTP 404 for {method} {url}")
                if response.status in _RETRY_STATUS:
                    raise _Transient(f"HTTP {response.status}", _retry_after(response))
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise DestinationError(f"OneDrive answered HTTP {response.status}: {detail}")
                if response.content_type == "application/json":
                    return await response.json()
                return await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            # The request never got an answer, so it may well get one next time.
            raise _Transient(f"{type(err).__name__}: {err}") from err

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        allow_missing: bool = False,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Make one Graph call, retrying the failures that are worth retrying.

        `allow_missing` turns 404 into None, which is what deleting something already gone
        and listing a folder that does not exist yet both want. It is **off by default**: a
        404 answering a write means the clip was not stored, and treating that as success is
        how a syncer comes to believe it holds footage that is not there.
        """
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                return await self._async_once(
                    method, url, allow_missing=allow_missing, data=data, headers=headers
                )
            except _Transient as err:
                if attempt == REQUEST_ATTEMPTS:
                    raise DestinationError(
                        f"OneDrive did not accept {method} after {attempt} attempts: {err}"
                    ) from err
                delay = err.retry_after or min(
                    BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS
                )
                _LOGGER.debug(
                    "OneDrive %s failed (%s); retrying in %.0fs (attempt %s of %s)",
                    method,
                    err,
                    delay,
                    attempt + 1,
                    REQUEST_ATTEMPTS,
                )
                await asyncio.sleep(delay)
        # Unreachable: the loop either returns or raises on its last attempt.
        raise DestinationError(f"OneDrive did not accept {method}")

    @staticmethod
    def _item(path: str) -> str:
        """Address one item inside the app folder by path."""
        # Graph's path addressing is `approot:/a/b/c:`; the colons are part of the syntax,
        # which is why a clip name containing one would break the URL rather than the file.
        return f"{APPROOT}:/{quote(path)}:"

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one clip, and confirm from Graph's own answer that it landed.

        Graph returns the stored item, so success is checked rather than assumed: an upload
        is only reported as done once the service has named the item it created and agreed on
        its length. Anything less and the caller would record a clip it does not have, which
        is worse than a reported failure — a failure gets retried.
        """
        if len(data) > SIMPLE_UPLOAD_LIMIT:
            raise UploadTooLargeError(
                f"{len(data)} bytes exceeds the {SIMPLE_UPLOAD_LIMIT} byte single-request limit"
            )
        item = await self._async_request(
            "PUT",
            f"{self._item(path)}/content",
            data=data,
            headers={"Content-Type": "video/mp4"},
        )
        if not isinstance(item, dict) or not item.get("id"):
            raise DestinationError(f"OneDrive did not confirm storing {path}")
        stored = item.get("size")
        if stored is not None and int(stored) != len(data):
            raise DestinationError(
                f"OneDrive stored {stored} bytes of {path}, not the {len(data)} sent"
            )
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def async_delete(self, path: str) -> None:
        """Remove one clip, treating an already-missing file as success."""
        await self._async_request("DELETE", self._item(path), allow_missing=True)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        found: dict[str, int] = {}
        url = f"{self._item(folder)}/children?$select=name,size&$top=200"
        while url:
            payload = await self._async_request("GET", url, allow_missing=True)
            if payload is None:
                return found  # the folder does not exist yet, which is not an error
            for item in payload.get("value", []):
                if "folder" in item:
                    continue
                found[f"{folder}/{item['name']}"] = int(item.get("size") or 0)
            url = payload.get("@odata.nextLink")
        return found
