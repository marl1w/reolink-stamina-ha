"""Synology DSM, through the File Station client its own integration already holds.

The closest thing to "record a copy to the NAS" for the boxes most people own: the Synology
integration is already there for its sensors, and its session is reused here rather than a
second login being asked for.

File Station addresses everything from a shared folder down, so the first segment of the
configured folder is the share — `home/reolink` writes to `/home/reolink`, and a share that
does not exist is an error the NAS reports rather than something this creates.

Nothing here imports `synology_dsm` at module level, so the destinations package stays
importable for the great majority of users who do not have a Synology.
"""

from __future__ import annotations

from collections.abc import Awaitable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .base import Destination, DestinationError, TransientError, async_attempt

_LOGGER = logging.getLogger(__name__)

# File Station pages its listings and defaults to a hundred entries. A folder of clips can
# be far longer than that, and a page it did not ask for is a clip the quota forgets.
PAGE = 500

# What DSM answers when the thing being deleted is not there: a File Station error (900)
# whose detail is "no such file or directory" (408).
_NO_SUCH_FILE = 900
_NO_SUCH_FILE_DETAIL = 408


def _is_missing(err: Exception) -> bool:
    """Return whether a DSM error means the thing asked for was not there.

    Two shapes, because the two APIs report it differently. Listing a folder that does not
    exist answers 408 outright; deleting a file that does not exist answers the batch
    failure 900, with the 408 one level down among the errors it collected.
    """
    args = err.args[0] if err.args else None
    if not isinstance(args, dict):
        return False
    code = int(args.get("code", 0))
    if code == _NO_SUCH_FILE_DETAIL:
        return True
    if code != _NO_SUCH_FILE:
        return False
    details = args.get("details")
    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        return False
    return int(details[0].get("code", 0)) == _NO_SUCH_FILE_DETAIL


class SynologyDestination(Destination):
    """Clips in a File Station shared folder."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded Synology DSM config entry."""
        self._hass = hass
        self._entry = entry

    @property
    def label(self) -> str:
        """Return the NAS this uploads to."""
        return f"Synology ({self._entry.title})"

    @property
    def _file_station(self) -> Any:
        """Return the File Station client the Synology integration keeps on its entry.

        File Station is a package the user may not have installed, and the integration
        leaves the attribute unset when it is missing — which is worth saying plainly here,
        because the alternative is a syncer that fails once per clip for a reason no log
        line explains.
        """
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")
        data = getattr(self._entry, "runtime_data", None)
        station = getattr(getattr(data, "api", None), "file_station", None)
        if station is None:
            raise DestinationError(
                f"{self._entry.title} has no File Station; install and enable it in DSM"
            )
        return station

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        """Split a clip path into the File Station folder it lives in and its name."""
        folder, _, name = path.rpartition("/")
        if not folder or not name:
            raise DestinationError(f"{path} is not a path inside a shared folder")
        return f"/{folder.strip('/')}", name

    async def _async_call[T](self, what: str, call: Awaitable[T]) -> T:
        """Await one File Station call, deciding whether its failure is worth a retry.

        The library raises one exception type for everything the API can report, so a
        connection that dropped and a share that does not exist arrive the same way. Both
        are retried; the second simply fails four times before it is reported, which costs
        a few seconds of a misconfiguration that a person has to fix anyway.
        """
        try:
            return await call
        except DestinationError:
            raise
        except Exception as err:
            raise TransientError(f"{type(err).__name__} during {what}: {err}") from err

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one clip, and confirm from DSM's own answer that it landed."""
        folder, name = self._split(path)

        async def attempt() -> None:
            stored = await self._async_call(
                f"upload of {path}",
                self._file_station.upload_file(
                    path=folder, filename=name, source=data, create_parents=True
                ),
            )
            if not stored:
                raise DestinationError(f"Synology did not confirm storing {path}")

        await async_attempt(f"Synology upload of {path}", attempt)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def async_delete(self, path: str) -> None:
        """Remove one clip, treating an already-missing file as success."""
        folder, name = self._split(path)

        async def attempt() -> None:
            # Resolved before the try: "File Station is not installed" is a settled fact
            # about the NAS, and retrying it four times only delays saying so.
            station = self._file_station
            try:
                await station.delete_file(path=folder, filename=name)
            except Exception as err:
                if _is_missing(err):
                    return
                raise TransientError(f"{type(err).__name__}: {err}") from err

        await async_attempt(f"Synology delete of {path}", attempt)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        remote = f"/{folder.strip('/')}"

        async def attempt() -> dict[str, int]:
            station = self._file_station
            found: dict[str, int] = {}
            offset = 0
            while True:
                try:
                    files = await station.get_files(path=remote, offset=offset, limit=PAGE)
                except Exception as err:
                    if _is_missing(err):
                        return found  # the folder does not exist yet, which is not an error
                    raise TransientError(f"{type(err).__name__}: {err}") from err
                if not files:
                    return found
                for item in files:
                    if item.is_dir:
                        continue
                    size = getattr(item.additional, "size", None)
                    found[f"{folder}/{item.name}"] = int(size or 0)
                if len(files) < PAGE:
                    return found
                offset += PAGE

        return await async_attempt(f"Synology listing of {folder}", attempt)
