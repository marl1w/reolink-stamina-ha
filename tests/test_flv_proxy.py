"""Tests for streaming a recording straight through to the browser."""

from __future__ import annotations

from base64 import urlsafe_b64decode
from urllib.parse import parse_qs, urlsplit

from custom_components.reolink_stamina.flv_proxy import (
    PLAYBACK_STREAM_TYPE,
    async_flv_path,
    async_playback_source,
)


def test_path_encodes_everything_needed_to_replay() -> None:
    """The whole reference lives in the path, so the panel can sign it unambiguously."""
    path = async_flv_path(
        "entry", 8, "sub", "1-8-0-01260703073001-00000", "20260803093001", "20260803073001", 240
    )

    _, api, domain, kind, entry, channel, stream, encoded, start, playback, seek = path.split("/")
    assert (api, domain, kind) == ("api", "reolink_stamina", "flv")
    assert (entry, channel, stream) == ("entry", "8", "sub")
    # The file name is encoded, because it is not URL-safe.
    assert urlsafe_b64decode(encoded.encode()).decode() == "1-8-0-01260703073001-00000"
    assert start == "20260803093001"
    assert playback == "20260803073001"
    assert seek == "240"


def test_a_negative_seek_never_reaches_the_path() -> None:
    """Nonsense offsets are clamped rather than passed on."""
    assert async_flv_path("e", 0, "sub", "f", "s", "p", -30).endswith("/0")


def test_the_sub_stream_is_type_one() -> None:
    """The recorder selects resolution numerically, per its own enum."""
    assert PLAYBACK_STREAM_TYPE["sub"] == 1
    assert PLAYBACK_STREAM_TYPE["main"] == 0


async def test_playback_source_sends_every_required_parameter(hass, patch_host) -> None:
    """Omitting any one of these makes the recorder 404 or drop the connection."""
    source = await async_playback_source(
        hass,
        "entry",
        8,
        "sub",
        "1-8-0-01260703073001-00000",
        "20260803093001",
        "20260803073001",
        240,
    )

    query = parse_qs(urlsplit(source).query)
    assert query["cmd"] == ["Playback"]
    assert query["channel"] == ["8"]
    assert query["type"] == ["1"]
    assert query["start"] == ["20260803093001"]
    assert query["seek"] == ["240"]
    assert query["source"] == ["1-8-0-01260703073001-00000"]
    # PlaybackTime is StartTime in UTC, and is required alongside it.
    assert query["playbackTime"] == ["20260803073001"]
    # The token comes from the library, so authentication stays its problem.
    assert query["token"] == ["TOK123"]
