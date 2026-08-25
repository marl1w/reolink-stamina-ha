"""Shared fixtures.

No test may require a real NVR. `FakeApi`/`FakeHost` stand in for reolink_aio, which
also makes them the canary for the adapter: if the real Reolink integration changes the
shape this fake imitates, the adapter tests fail loudly.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from reolink_aio.typings import VOD_trigger

from custom_components.reolink_stamina.const import JOURNAL_FILENAME

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable in every test."""
    return enable_custom_integrations


@pytest.fixture(autouse=True)
def clean_journal(hass):
    """Remove the relevance journal after every test.

    The test harness shares one configuration directory across the whole run, and the
    journal is deliberately a real file in it — so every test that sets the integration up
    leaves one behind for every test that follows. Removal is supposed to delete it, and an
    assertion about that must not be able to pass or fail by accident.
    """
    yield
    for suffix in ("", "-wal", "-shm"):
        Path(f"{hass.config.path(JOURNAL_FILENAME)}{suffix}").unlink(missing_ok=True)


class FakeVodFile:
    """Stands in for reolink_aio's VOD_file."""

    def __init__(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        name: str = "Mp4Record/2026-08-03/RecM01_20260803_140000_140030_0_800.mp4",
        size: int = 1024,
        triggers: VOD_trigger = VOD_trigger.NONE,
        stream_type: str = "sub",
    ) -> None:
        self.start_time = start
        self.end_time = end
        self.file_name = name
        self.size = size
        self.triggers = triggers
        self.type = stream_type

    @property
    def duration(self) -> dt.timedelta:
        return self.end_time - self.start_time

    @property
    def playback_time(self) -> dt.datetime:
        # The device reports this in UTC; it is what the playback endpoint keys on.
        return self.start_time.astimezone(dt.UTC)

    @property
    def start_time_id(self) -> str:
        return self.start_time.strftime("%Y%m%d%H%M%S")

    @property
    def end_time_id(self) -> str:
        return self.end_time.strftime("%Y%m%d%H%M%S")


class FakeSearchStatus:
    """Stands in for reolink_aio's VOD_search_status."""

    def __init__(self, year: int, month: int, days: tuple[int, ...]) -> None:
        self.year = year
        self.month = month
        self.days = days


class FakeApi:
    """A minimal stand-in for reolink_aio's Host API."""

    def __init__(
        self,
        *,
        is_nvr: bool = True,
        is_hub: bool = False,
        channels: list[int] | None = None,
        files: dict[str, list[FakeVodFile]] | None = None,
        statuses: list[FakeSearchStatus] | None = None,
    ) -> None:
        self.is_nvr = is_nvr
        self.is_hub = is_hub
        self.is_dual_lens = False
        self.model = "RLN8-410"
        self.sw_version = "v3.5.1"
        self.nvr_name = "Test NVR"
        self.hdd_info = [{"size": 1000, "used": 500}]
        self._channels = channels if channels is not None else [0, 1]
        self._files = files or {}
        self._statuses = statuses or []
        self.search_calls: list[dict[str, Any]] = []
        self.baichuan = _FakeBaichuan()
        self.vod_source_calls: list[dict[str, Any]] = []
        # Private on the real Host, and read as such by redact.api_secrets. The password
        # carries an `&` on purpose: it is what a pattern-only scrub truncates.
        self._username = "admin"
        self._password = "s3cr&t"
        self._enc_password = ""
        self._token = "TOK123"

    @property
    def channels(self) -> list[int]:
        return self._channels

    @property
    def stream_channels(self) -> list[int]:
        return self._channels

    def camera_name(self, channel: int | None) -> str:
        return self.nvr_name if channel is None else f"Camera {channel}"

    def supported(self, channel: int | None, capability: str) -> bool:
        return capability in {"replay"}

    def ai_supported_types(self, channel: int) -> list[str]:
        return ["person", "vehicle"]

    async def get_vod_source(self, channel, filename, stream=None, request_type=None):
        from reolink_aio.enums import VodRequestType

        self.vod_source_calls.append(
            {
                "channel": channel,
                "filename": filename,
                "stream": stream,
                "request_type": request_type,
            }
        )
        if request_type == VodRequestType.FLV:
            # Shaped like reolink_aio's FLV URL: seek pinned to zero, credentials in the
            # query unencoded, the recording named by `start`. Used whole, with only the
            # seek replaced.
            stream_type = 1 if stream == "sub" else 0
            return (
                "application/x-mpegURL",
                f"http://nvr:80/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}"
                f"&type={stream_type}&start={filename}&seek=0"
                f"&user={self._username}&password={self._password}",
            )
        if request_type == VodRequestType.DOWNLOAD:
            return "video/mp4", f"http://nvr/download?source={filename}&token=TOK123"
        # Shaped like reolink_aio's PLAYBACK URL: only its base and token are used.
        return (
            "video/mp4",
            f"http://nvr/cgi-bin/api.cgi?cmd=Playback&source={filename}&output=x.mp4&token=TOK123",
        )

    def hide_password(self, text):
        return str(text).replace(self._password, "<password>")

    async def request_vod_files(
        self,
        channel: int,
        start: dt.datetime,
        end: dt.datetime,
        status_only: bool = False,
        stream: str | None = None,
        split_time: dt.timedelta | None = None,
        trigger: VOD_trigger | None = None,
    ) -> tuple[list[FakeSearchStatus], list[FakeVodFile]]:
        self.search_calls.append(
            {
                "channel": channel,
                "start": start,
                "end": end,
                "status_only": status_only,
                "stream": stream,
                "split_time": split_time,
            }
        )
        if status_only:
            return self._statuses, []
        return [], list(self._files.get(stream or "sub", []))


class _Absorbed:
    """The result of any Baichuan call, whether the caller awaits it or drops it.

    `__getattr__` cannot know which it will be: the real Baichuan side is mostly
    coroutines, but `unregister_callback` is synchronous and the unload path calls it
    bare. Handing back a coroutine for that one is a `RuntimeWarning` per call — it is
    never awaited — so this is awaitable without being a coroutine, and both uses are
    silent.
    """

    def __await__(self):
        return iter(())


class _FakeBaichuan:
    """Stand-in for reolink_aio's Baichuan side, which the real unload path touches."""

    def __getattr__(self, name: str):
        def _noop(*args: Any, **kwargs: Any) -> _Absorbed:
            return _Absorbed()

        return _noop


class FakeHost:
    """Stands in for the Reolink integration's ReolinkHost."""

    def __init__(self, api: FakeApi) -> None:
        self.api = api

    async def stop(self) -> None:
        """Absorb the unload the real integration performs during test teardown."""


@pytest.fixture
def fake_api() -> FakeApi:
    """Return a fake NVR API with no recordings."""
    return FakeApi()


@pytest.fixture
def patch_host(fake_api: FakeApi):
    """Patch the adapter so every lookup returns the fake host."""
    host = FakeHost(fake_api)
    with (
        patch(
            "custom_components.reolink_stamina.vod.async_get_host",
            return_value=host,
        ),
        patch(
            "custom_components.reolink_stamina.reolink_registry.async_get_host",
            return_value=host,
        ),
        # Imported by name, so each module that uses it needs patching in its own right.
        patch(
            "custom_components.reolink_stamina.playback_route.async_get_host",
            return_value=host,
        ),
        patch(
            "custom_components.reolink_stamina.api.playback.async_get_host",
            return_value=host,
        ),
    ):
        yield host
