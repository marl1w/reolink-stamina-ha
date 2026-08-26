"""OneDrive, through the OAuth session its own integration already holds.

The cost of borrowing that session is the scope the integration holds —
`Files.ReadWrite.AppFolder` — so clips live inside Home Assistant's own app folder rather
than at the root of the drive.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .base import DestinationError, UploadTooLargeError
from .oauth import OAuthDestination

_LOGGER = logging.getLogger(__name__)

# Microsoft Graph. The app folder is addressed by name, so nothing here needs the drive id.
GRAPH = "https://graph.microsoft.com/v1.0"
APPROOT = f"{GRAPH}/me/drive/special/approot"

# Graph accepts a single PUT up to 250 MB; clips are measured in single-digit megabytes, so
# the resumable session protocol would be ceremony for nothing.
SIMPLE_UPLOAD_LIMIT = 240 * 1024 * 1024


class OneDriveDestination(OAuthDestination):
    """Clips in Home Assistant's OneDrive app folder."""

    service = "OneDrive"

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
