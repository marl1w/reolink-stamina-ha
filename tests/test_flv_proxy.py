"""Tests for streaming a recording straight through to the browser."""

from __future__ import annotations

from base64 import urlsafe_b64decode
from urllib.parse import parse_qs, urlsplit

from custom_components.reolink_stamina.flv_proxy import (
    PLAYBACK_STREAM_TYPE,
    async_flv_path,
    async_playback_source,
    scrub_credentials,
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


async def test_camera_playback_source_sends_every_required_parameter(
    hass, fake_api, patch_host
) -> None:
    """Omitting any one of these makes the camera 404 or drop the connection."""
    fake_api.is_nvr = False
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


async def test_nvr_playback_source_is_the_flv_route(hass, patch_host) -> None:
    """An NVR does not implement `cmd=Playback`; its playback lives at `/flv`.

    The library's URL is used whole — it knows the ports, the stream type and which
    credentials this endpoint takes — with only its pinned `seek=0` replaced by where
    playback should begin.
    """
    source = await async_playback_source(
        hass,
        "entry",
        9,
        "sub",
        "20260807200001",
        "20260807130001",
        "20260807200001",
        1718,
    )

    parts = urlsplit(source)
    assert parts.path == "/flv"
    query = parse_qs(parts.query)
    assert query["stream"] == ["playback.bcs"]
    assert query["channel"] == ["9"]
    assert query["type"] == ["1"]
    # The file is named by `start` — the recording's own name, not a wall clock.
    assert query["start"] == ["20260807200001"]
    assert query["seek"] == ["1718"]
    # Everything else the library said survives byte for byte.
    assert query["port"] == ["1935"]
    assert query["user"] == ["admin"]
    assert query["password"] == ["p"]


async def test_nvr_playback_source_keeps_a_zero_seek(hass, patch_host) -> None:
    """Replacing seek=0 with seek=0 must still leave exactly one seek parameter."""
    source = await async_playback_source(hass, "entry", 9, "sub", "20260807200001", "s", "p", 0)
    assert parse_qs(urlsplit(source).query)["seek"] == ["0"]


async def test_hub_playback_source_takes_the_camera_path(hass, fake_api, patch_host) -> None:
    """A Home Hub answers `cmd=Playback` the way the cameras do."""
    fake_api.is_hub = True
    source = await async_playback_source(
        hass, "entry", 0, "sub", "file.mp4", "20260803093001", "20260803073001", 0
    )
    assert parse_qs(urlsplit(source).query)["cmd"] == ["Playback"]


def test_scrub_credentials_blanks_every_secret_and_nothing_else() -> None:
    """What ffmpeg quotes back must never carry the recorder's credentials."""
    said = (
        "Error opening input file http://nvr/flv?port=1935&app=bcs&stream=playback.bcs"
        "&channel=9&type=1&start=20260807200001&seek=1718&user=admin&password=s3cr&t"
        " and also token=c131a8bbf74fc0d elsewhere."
    )
    scrubbed = scrub_credentials(said)
    assert "s3cr" not in scrubbed
    assert "admin" not in scrubbed
    assert "c131a8bbf74fc0d" not in scrubbed
    assert "user=***&password=***" in scrubbed
    # The parts that explain the failure survive.
    assert "start=20260807200001" in scrubbed
    assert "seek=1718" in scrubbed
