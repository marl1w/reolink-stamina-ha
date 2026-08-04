"""Tests for getting a clip into the cloud, and knowing whether it arrived.

The bytes were expensive: reading them cost the recorder a real-time playback it can only do
one of at a time. So a transient failure is waited out here rather than thrown back, and a
success is only reported once the service has said what it stored — because a syncer that
believes it holds footage it does not hold is worse than one that reports a failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.reolink_stamina.cloud.destination import (
    REQUEST_ATTEMPTS,
    DestinationError,
    OneDriveDestination,
    UploadTooLargeError,
)

MOD = "custom_components.reolink_stamina.cloud.destination"

PATH = "Reolink/Main House/260804_215051_main-nvr_Front Gate.mp4"
CLIP = b"x" * 2048


class FakeResponse:
    """One Graph answer, shaped the way aiohttp presents it."""

    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        """Record what this answer says."""
        self.status = status
        self.headers = headers or {}
        self._payload = payload
        self._text = text
        self.content_type = "application/json" if payload is not None else "application/octet"

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return b""

    async def __aenter__(self) -> FakeResponse:
        """Enter the context aiohttp's request() returns."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave it, releasing nothing in particular."""
        return None


class FakeSession:
    """A client session that answers from a script and records what it was asked."""

    def __init__(self, answers: list[Any]) -> None:
        """Answer with each item in turn; the last is repeated if asked again."""
        self._answers = answers
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_destination(answers: list[Any]) -> tuple[OneDriveDestination, FakeSession]:
    """Return a destination wired to a scripted session, with tokens taken as read."""
    entry = MagicMock(title="OneDrive", entry_id="onedrive-entry")
    destination = OneDriveDestination(MagicMock(), entry)
    destination._async_token = AsyncMock(return_value="token")
    return destination, FakeSession(answers)


def stored(size: int, item_id: str = "01ABC") -> FakeResponse:
    """Return the answer Graph gives when it has written the file."""
    return FakeResponse(200, {"id": item_id, "name": "clip.mp4", "size": size})


async def test_a_confirmed_upload_succeeds() -> None:
    """The ordinary case: Graph names the item it created and agrees on its length."""
    destination, session = make_destination([stored(len(CLIP))])

    with patch(f"{MOD}.async_get_clientsession", return_value=session):
        await destination.async_store(PATH, CLIP)

    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "PUT"
    assert session.calls[0]["data"] == CLIP
    assert session.calls[0]["headers"]["Content-Type"] == "video/mp4"


async def test_a_404_on_upload_is_a_failure_not_a_success() -> None:
    """The bug this exists for.

    404 was mapped to None for every verb, and `async_store` ignored the return — so a PUT
    that stored nothing returned quietly, and the syncer went on to record the clip in its
    index, count it as uploaded and clear its last error. A sync reporting perfect health with
    an empty folder behind it is the one failure that cannot be noticed.
    """
    destination, session = make_destination([FakeResponse(404, text="itemNotFound")])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        pytest.raises(DestinationError, match="404"),
    ):
        await destination.async_store(PATH, CLIP)

    assert len(session.calls) == 1, "a 404 is permanent; retrying it would only waste time"


async def test_an_upload_graph_does_not_confirm_is_a_failure() -> None:
    """No item id means no evidence the clip is there."""
    destination, session = make_destination([FakeResponse(200, {})])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        pytest.raises(DestinationError, match="did not confirm"),
    ):
        await destination.async_store(PATH, CLIP)


async def test_a_truncated_upload_is_a_failure() -> None:
    """A stored length that disagrees with what was sent means a damaged clip."""
    destination, session = make_destination([stored(len(CLIP) // 2)])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        pytest.raises(DestinationError, match="not the"),
    ):
        await destination.async_store(PATH, CLIP)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_a_throttled_or_broken_gateway_is_retried(status: int) -> None:
    """Graph throttles, and a service that size produces gateway errors that mean nothing."""
    destination, session = make_destination([FakeResponse(status), stored(len(CLIP))])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        patch(f"{MOD}.asyncio.sleep", new=AsyncMock()) as slept,
    ):
        await destination.async_store(PATH, CLIP)

    assert len(session.calls) == 2, "the second attempt is what gets the clip there"
    assert slept.await_count == 1


async def test_graphs_own_retry_after_is_obeyed() -> None:
    """A throttled caller is told how long to wait; ignoring it earns another 429."""
    destination, session = make_destination(
        [FakeResponse(429, headers={"Retry-After": "7"}), stored(len(CLIP))]
    )

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        patch(f"{MOD}.asyncio.sleep", new=AsyncMock()) as slept,
    ):
        await destination.async_store(PATH, CLIP)

    slept.assert_awaited_once_with(7.0)


async def test_retrying_does_not_lose_the_clip_or_its_headers() -> None:
    """Every attempt must send the same bytes.

    The headers used to be taken out of the keyword arguments with `pop`, which emptied them
    for every attempt after the first — so a retry uploaded the clip without its content type.
    """
    destination, session = make_destination(
        [FakeResponse(503), FakeResponse(503), stored(len(CLIP))]
    )

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        patch(f"{MOD}.asyncio.sleep", new=AsyncMock()),
    ):
        await destination.async_store(PATH, CLIP)

    assert len(session.calls) == 3
    for call in session.calls:
        assert call["data"] == CLIP
        assert call["headers"]["Content-Type"] == "video/mp4"
        assert call["headers"]["Authorization"] == "Bearer token"


async def test_retries_are_not_endless() -> None:
    """A service that is simply down must be reported, not waited on for ever."""
    destination, session = make_destination([FakeResponse(503)])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        patch(f"{MOD}.asyncio.sleep", new=AsyncMock()),
        pytest.raises(DestinationError, match="attempts"),
    ):
        await destination.async_store(PATH, CLIP)

    assert len(session.calls) == REQUEST_ATTEMPTS


async def test_a_clip_too_large_is_refused_without_asking() -> None:
    """No point sending 300 MB to an endpoint that takes 240."""
    destination, session = make_destination([stored(1)])

    with (
        patch(f"{MOD}.async_get_clientsession", return_value=session),
        pytest.raises(UploadTooLargeError),
    ):
        await destination.async_store(PATH, b"x" * (241 * 1024 * 1024))

    assert session.calls == []


async def test_deleting_something_already_gone_succeeds() -> None:
    """Eviction must not fail because someone tidied the folder by hand."""
    destination, session = make_destination([FakeResponse(404)])

    with patch(f"{MOD}.async_get_clientsession", return_value=session):
        await destination.async_delete(PATH)

    assert session.calls[0]["method"] == "DELETE"


async def test_listing_a_folder_that_does_not_exist_yet_is_empty() -> None:
    """A new area has no folder until its first clip; that is not an error."""
    destination, session = make_destination([FakeResponse(404)])

    with patch(f"{MOD}.async_get_clientsession", return_value=session):
        assert await destination.async_list("Reolink/Main House") == {}


async def test_listing_returns_paths_and_sizes_and_skips_folders() -> None:
    """The index is reconciled against this, so the keys have to match stored paths."""
    payload = {
        "value": [
            {"name": "260804_215051_main-nvr_Front Gate.mp4", "size": 2048},
            {"name": "a-subfolder", "size": 0, "folder": {"childCount": 0}},
        ]
    }
    destination, session = make_destination([FakeResponse(200, payload)])

    with patch(f"{MOD}.async_get_clientsession", return_value=session):
        found = await destination.async_list("Reolink/Main House")

    assert found == {"Reolink/Main House/260804_215051_main-nvr_Front Gate.mp4": 2048}
