"""What a destination has to be, and the one thing they all get wrong the same way.

A clip is bytes with a path, and a destination can store one, delete one, and say what it
already holds. Everything else — quotas, eviction, naming, deciding which events become
clips — is the syncer's business, so adding a provider means implementing three methods
rather than understanding the whole feature.

Getting a clip *there* is the implementations' problem, though, and it is treated as one:
the bytes cost a real-time playback from the recorder to obtain, so a throttled or briefly
unavailable service is waited out and retried here rather than thrown back for the whole
clip to be fetched again. `async_store` returns only once the service has confirmed what it
stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable
import logging

_LOGGER = logging.getLogger(__name__)

# A clip's bytes were expensive to get — reading them cost the recorder a real-time
# playback — so an operation that can succeed on a second attempt should not cost that again.
REQUEST_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0


class DestinationError(Exception):
    """Raised when a destination cannot do what was asked of it."""


class UploadTooLargeError(DestinationError):
    """Raised when a clip exceeds what this destination will take in one go."""


class TransientError(Exception):
    """A failure worth retrying, with the service's own wait if it named one.

    Never raised out of a destination: `async_attempt` either succeeds or converts the last
    one into a `DestinationError`, so callers see one failure type.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record what failed and how long to wait."""
        super().__init__(message)
        self.retry_after = retry_after


async def async_attempt[T](
    what: str,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = REQUEST_ATTEMPTS,
) -> T:
    """Run one destination operation until it stops failing transiently.

    `operation` is a factory rather than a coroutine because a retried request has to be
    built again: a consumed body or an expired token cannot be sent twice.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except TransientError as err:
            if attempt == attempts:
                raise DestinationError(f"{what} failed after {attempt} attempts: {err}") from err
            delay = err.retry_after or min(
                BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS
            )
            _LOGGER.debug(
                "%s failed (%s); retrying in %.0fs (attempt %s of %s)",
                what,
                err,
                delay,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(delay)
    # Unreachable: the loop either returns or raises on its last attempt.
    raise DestinationError(f"{what} failed")


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

    async def async_close(self) -> None:  # noqa: B027 - deliberately optional
        """Let go of anything held open. Called when the syncer stops.

        Most providers hold nothing between calls and need not override this; the one that
        keeps a connection open would otherwise leak it on every reload.
        """
