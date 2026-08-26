"""SFTP, reusing the credentials the SFTP Storage integration already validated.

Unlike the other providers, that integration keeps only its settings on the config entry —
its client is built per backup — so this opens its own connection from the same host, port,
user and key. Nothing is asked of the user twice, and a key they rotate in one place is
rotated for both.

One connection is held open for the life of the syncer, because opening an SSH session per
clip would cost more than sending one. Any failure drops it, so the retry that follows
reconnects rather than pushing more bytes down a socket that has already gone.

Clips live under the integration's own backup location: it is the one directory on that
server Home Assistant has already proved it can write to.
"""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import suppress
import logging
import stat
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .base import Destination, DestinationError, TransientError, async_attempt

_LOGGER = logging.getLogger(__name__)


def _classify(what: str, err: Exception) -> Exception:
    """Decide whether one SFTP failure is worth another try.

    A refused login or a rejected path will be refused again; a dropped connection or a
    server that ran out of handles will very likely not. The imports are done here rather
    than at module level so that this package stays importable without `asyncssh`.
    """
    from asyncssh import PermissionDenied
    from asyncssh.sftp import (
        SFTPFailure,
        SFTPNoSuchFile,
        SFTPPermissionDenied,
    )

    if isinstance(err, (PermissionDenied, SFTPPermissionDenied, SFTPNoSuchFile, SFTPFailure)):
        return DestinationError(f"SFTP refused the {what}: {err}")
    return TransientError(f"{type(err).__name__}: {err}")


class SftpDestination(Destination):
    """Clips on an SFTP server, under the folder the syncer was configured with."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded SFTP Storage config entry."""
        self._hass = hass
        self._entry = entry
        self._ssh: Any = None
        self._sftp: Any = None

    @property
    def label(self) -> str:
        """Return the server this uploads to."""
        return f"SFTP ({self._entry.title})"

    @property
    def _config(self) -> Any:
        """Return the host, credentials and root the SFTP integration was set up with."""
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")
        config = getattr(self._entry, "runtime_data", None)
        if config is None:
            raise DestinationError(f"{self._entry.title} has no SFTP configuration")
        return config

    def _remote(self, path: str) -> str:
        """Return one clip path as the server sees it, under the configured root."""
        return f"{str(self._config.backup_location).rstrip('/')}/{path}"

    async def _async_sftp(self) -> Any:
        """Return the open SFTP client, connecting on first use and after a drop."""
        if self._sftp is not None:
            return self._sftp

        from asyncssh import connect
        from homeassistant.components.sftp_storage.client import (
            get_client_options,
        )

        config = self._config
        try:
            # Building the options parses a private key from disk, which is why the SFTP
            # integration does it off the event loop too.
            options = await self._hass.async_add_executor_job(get_client_options, config)
            self._ssh = await connect(host=config.host, port=config.port, options=options)
            self._sftp = await self._ssh.start_sftp_client()
        except Exception as err:
            await self._async_disconnect()
            raise _classify("connection", err) from err
        return self._sftp

    async def _async_disconnect(self) -> None:
        """Drop the connection, so the next call builds a fresh one.

        Closing a socket that is already broken can itself raise, and this runs both after a
        failure and when the integration is unloaded — where an exception would leave the
        rest of the teardown undone for the sake of a connection that is going away anyway.
        """
        sftp, ssh, self._sftp, self._ssh = self._sftp, self._ssh, None, None
        with suppress(Exception):
            if sftp is not None:
                sftp.exit()
                await sftp.wait_closed()
        with suppress(Exception):
            if ssh is not None:
                ssh.close()
                await ssh.wait_closed()

    async def _async_call[T](self, what: str, call: Awaitable[T]) -> T:
        """Await one client call, dropping the connection if it was the connection's fault."""
        try:
            return await call
        except DestinationError:
            raise
        except Exception as err:
            failure = _classify(what, err)
            if isinstance(failure, TransientError):
                await self._async_disconnect()
            raise failure from err

    async def async_close(self) -> None:
        """Let go of the connection when the syncer stops."""
        await self._async_disconnect()

    async def async_store(self, path: str, data: bytes) -> None:
        """Write one clip, and confirm from the server's own answer that it landed.

        The size is read back rather than assumed: a write cut short by a connection that
        died mid-transfer leaves a short file the server is perfectly happy with.
        """
        remote = self._remote(path)
        folder, _, _ = remote.rpartition("/")

        async def attempt() -> None:
            sftp = await self._async_sftp()
            if folder:
                await self._async_call(
                    f"creation of {folder}", sftp.makedirs(folder, exist_ok=True)
                )
            handle = await self._async_call(f"open of {remote}", sftp.open(remote, "wb"))
            try:
                await self._async_call(f"write of {remote}", handle.write(data))
            finally:
                # A write that failed has already torn the connection down, so closing its
                # handle raises in turn — and an exception from here would replace the
                # transient failure that the retry above is waiting for.
                with suppress(Exception):
                    await handle.close()
            attrs = await self._async_call(f"check of {remote}", sftp.stat(remote))
            if attrs.size is not None and int(attrs.size) != len(data):
                raise DestinationError(
                    f"SFTP stored {attrs.size} bytes of {path}, not the {len(data)} sent"
                )

        await async_attempt(f"SFTP write of {path}", attempt)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def async_delete(self, path: str) -> None:
        """Remove one clip, treating an already-missing file as success."""
        remote = self._remote(path)

        async def attempt() -> None:
            sftp = await self._async_sftp()
            if not await self._async_call(f"check of {remote}", sftp.exists(remote)):
                return
            await self._async_call(f"delete of {remote}", sftp.unlink(remote))

        await async_attempt(f"SFTP delete of {path}", attempt)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        remote = self._remote(folder)

        async def attempt() -> dict[str, int]:
            sftp = await self._async_sftp()
            if not await self._async_call(f"check of {remote}", sftp.isdir(remote)):
                return {}  # the folder does not exist yet, which is not an error
            entries = await self._async_call(f"listing of {remote}", sftp.readdir(remote))
            found: dict[str, int] = {}
            for entry in entries:
                name = entry.filename
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
                if name in (".", "..") or stat.S_ISDIR(entry.attrs.permissions or 0):
                    continue
                found[f"{folder}/{name}"] = int(entry.attrs.size or 0)
            return found

        return await async_attempt(f"SFTP listing of {folder}", attempt)
