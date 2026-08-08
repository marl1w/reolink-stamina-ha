"""Tests for choosing, remembering and addressing a recorder's playback endpoint."""

from __future__ import annotations

from base64 import urlsafe_b64decode
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.reolink_stamina.playback_route import (
    PLAYBACK_STREAM_TYPE,
    ROUTE_FLV,
    ROUTE_PLAYBACK,
    PlaybackRouteError,
    Recording,
    _with_seek,
    async_all_routes,
    async_forget_routes,
    async_open_playback_stream,
    async_playback_secrets,
    async_playback_source,
    async_remember_route,
    async_remembered_route,
    async_route_url,
)

RECORDING = Recording(
    entry_id="entry",
    channel=8,
    stream="sub",
    filename="1-8-0-01260703073001-00000",
    start_id="20260803093001",
    playback_id="20260803073001",
    seek=240,
)


class _FakeResponse:
    """One upstream answer, with the closing the caller owes it."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.closed = False
        self.released = False

    def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self.released = True


class _FakeSession:
    """Answers each URL according to which endpoint it names."""

    def __init__(self, *, playback: int | Exception, flv: int | Exception) -> None:
        self._answers = {ROUTE_PLAYBACK: playback, ROUTE_FLV: flv}
        self.requested: list[str] = []

    async def get(self, url: str, **_kwargs):
        self.requested.append(url)
        route = ROUTE_FLV if "/flv?" in url else ROUTE_PLAYBACK
        answer = self._answers[route]
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)


@pytest.fixture
def patch_session(monkeypatch):
    """Install a fake aiohttp session and hand the test its answers."""

    def install(*, playback: int | Exception, flv: int | Exception) -> _FakeSession:
        session = _FakeSession(playback=playback, flv=flv)
        monkeypatch.setattr(
            "custom_components.reolink_stamina.playback_route.async_get_clientsession",
            lambda _hass: session,
        )
        return session

    return install


# ------------------------------------------------------------------------- addressing


def test_the_sub_stream_is_type_one() -> None:
    """The recorder selects resolution numerically, per its own enum."""
    assert PLAYBACK_STREAM_TYPE["sub"] == 1
    assert PLAYBACK_STREAM_TYPE["main"] == 0


def test_a_recording_round_trips_through_its_path() -> None:
    """The whole reference lives in the path, so the panel can sign it unambiguously."""
    path = RECORDING.path("/api/reolink_stamina/flv")
    segments = path.removeprefix("/api/reolink_stamina/flv/").split("/")
    entry, channel, stream, encoded, start, playback, seek = segments

    assert (entry, channel, stream) == ("entry", "8", "sub")
    # The file name is encoded, because it is not URL-safe.
    assert urlsafe_b64decode(encoded.encode()).decode() == RECORDING.filename
    assert (start, playback, seek) == ("20260803093001", "20260803073001", "240")
    assert Recording.from_path(*segments) == RECORDING


def test_a_negative_seek_never_reaches_the_path() -> None:
    """Nonsense offsets are clamped rather than passed on."""
    nonsense = Recording("e", 0, "sub", "f", "s", "p", -30)
    assert nonsense.path("/x").endswith("/0")
    assert Recording.from_path("e", "0", "sub", "Zg==", "s", "p", "-30").seek == 0


def test_a_malformed_reference_is_refused() -> None:
    """What both views turn into a 400 rather than a request to the recorder."""
    with pytest.raises(ValueError):
        Recording.from_path("e", "not-a-channel", "sub", "Zg==", "s", "p", "0")


async def test_the_playback_route_sends_every_required_parameter(hass, patch_host) -> None:
    """Omitting any one of these makes the recorder 404 or drop the connection."""
    source = await async_route_url(hass, RECORDING, ROUTE_PLAYBACK)

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


async def test_the_flv_route_uses_the_library_url_whole(hass, patch_host) -> None:
    """The library knows the ports, the stream type and which credentials this takes."""
    source = await async_route_url(hass, RECORDING, ROUTE_FLV)

    parts = urlsplit(source)
    assert parts.path == "/flv"
    query = parse_qs(parts.query)
    assert query["stream"] == ["playback.bcs"]
    assert query["channel"] == ["8"]
    assert query["type"] == ["1"]
    # This route names the recording by file name, not by a wall clock.
    assert query["start"] == ["1-8-0-01260703073001-00000"]
    # Only the pinned seek is replaced; everything else survives byte for byte.
    assert query["seek"] == ["240"]
    assert query["port"] == ["1935"]
    assert query["user"] == ["admin"]


def test_the_flv_url_never_carries_two_seeks() -> None:
    """A library that stops pinning `seek=0` must not produce `seek=0&seek=240`.

    The substitution matches any value rather than the pinned zero, so a library that
    starts computing the seek is overridden. Appending only runs when the parameter is
    absent altogether, which is the only case that could ever duplicate it.
    """
    for template in (
        "http://nvr/flv?a=1&seek=0&user=admin",
        "http://nvr/flv?a=1&seek=17&user=admin",
        "http://nvr/flv?a=1&user=admin",
        "http://nvr/flv?seek=0",
    ):
        assert parse_qs(urlsplit(_with_seek(template, 240)).query)["seek"] == ["240"]


async def test_a_zero_seek_still_says_so(hass, patch_host) -> None:
    """Replacing seek=0 with seek=0 must leave exactly one seek parameter."""
    still = Recording("entry", 8, "sub", "f", "s", "p", 0)
    assert parse_qs(urlsplit(await async_route_url(hass, still, ROUTE_FLV)).query)["seek"] == ["0"]


# ----------------------------------------------------------------------------- routing


async def test_the_playback_route_is_tried_first(hass, patch_host, patch_session) -> None:
    """The route that has always worked is the default, not one of two equals."""
    session = patch_session(playback=200, flv=200)

    opened = await async_open_playback_stream(hass, RECORDING)

    assert opened.route == ROUTE_PLAYBACK
    assert len(session.requested) == 1
    assert "cmd=Playback" in session.requested[0]
    assert async_remembered_route(hass, "entry") == ROUTE_PLAYBACK


async def test_a_404_falls_back_to_the_flv_route(hass, patch_host, patch_session) -> None:
    """What an RLN16-410 does: 404 for every `cmd=Playback` however it is phrased."""
    session = patch_session(playback=404, flv=200)

    opened = await async_open_playback_stream(hass, RECORDING)

    assert opened.route == ROUTE_FLV
    assert len(session.requested) == 2
    assert async_remembered_route(hass, "entry") == ROUTE_FLV


async def test_a_dropped_connection_falls_back_too(hass, patch_host, patch_session) -> None:
    """A recorder that closes without answering is a refusal, not an error to report."""
    patch_session(playback=ConnectionResetError("Server disconnected"), flv=200)

    opened = await async_open_playback_stream(hass, RECORDING)

    assert opened.route == ROUTE_FLV


async def test_a_remembered_route_is_not_measured_again(hass, patch_host, patch_session) -> None:
    """The measurement costs one refused request per device, not one per recording."""
    session = patch_session(playback=404, flv=200)
    await async_open_playback_stream(hass, RECORDING)
    assert len(session.requested) == 2

    await async_open_playback_stream(hass, RECORDING)

    # The second open went straight to the endpoint that answered.
    assert len(session.requested) == 3
    assert "/flv?" in session.requested[-1]


async def test_a_remembered_route_still_falls_back(hass, patch_host, patch_session) -> None:
    """A recorder that stops answering yesterday's endpoint should not simply fail."""
    async_remember_route(hass, "entry", ROUTE_FLV)
    session = patch_session(playback=200, flv=500)

    opened = await async_open_playback_stream(hass, RECORDING)

    assert opened.route == ROUTE_PLAYBACK
    assert len(session.requested) == 2
    # And the correction sticks.
    assert async_remembered_route(hass, "entry") == ROUTE_PLAYBACK


async def test_both_endpoints_refusing_is_one_readable_error(
    hass, patch_host, patch_session
) -> None:
    """Two refusals are one diagnosis, and neither may quote a credential."""
    patch_session(playback=404, flv=ConnectionResetError("cannot connect: password=s3cr&t"))

    with pytest.raises(PlaybackRouteError) as raised:
        await async_open_playback_stream(hass, RECORDING)

    message = str(raised.value)
    assert "playback: HTTP 404" in message
    assert "s3cr&t" not in message
    assert async_remembered_route(hass, "entry") is None


async def test_ffmpeg_gets_a_url_on_the_measured_route(hass, patch_host, patch_session) -> None:
    """The URL handed to ffmpeg is the one that was just proven to work."""
    patch_session(playback=404, flv=200)

    source = await async_playback_source(hass, RECORDING)

    assert "/flv?" in source
    assert async_remembered_route(hass, "entry") == ROUTE_FLV


async def test_a_known_route_costs_ffmpeg_no_request(hass, patch_host, patch_session) -> None:
    """Once measured, building a URL talks to nothing."""
    async_remember_route(hass, "entry", ROUTE_PLAYBACK)
    session = patch_session(playback=200, flv=200)

    source = await async_playback_source(hass, RECORDING)

    assert "cmd=Playback" in source
    assert session.requested == []


async def test_routes_are_forgotten_on_demand(hass, patch_host) -> None:
    """A reload re-measures rather than trusting an answer from before a firmware update."""
    async_remember_route(hass, "entry", ROUTE_FLV)
    assert async_all_routes(hass) == {"entry": ROUTE_FLV}

    async_forget_routes(hass)

    assert async_all_routes(hass) == {}
    assert async_remembered_route(hass, "entry") is None


async def test_the_credentials_a_recorder_uses_are_readable(hass, patch_host) -> None:
    """What gets handed to a long-lived ffmpeg so its output can be scrubbed exactly."""
    assert "s3cr&t" in async_playback_secrets(hass, "entry")


async def test_an_unreadable_device_costs_the_literal_pass_only(hass) -> None:
    """A device that cannot be read must not take the conversion down with it."""
    assert async_playback_secrets(hass, "no-such-entry") == ()
