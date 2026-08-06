"""Tests for the adaptive playback beta.

Two things matter here and both are asserted without ever starting ffmpeg: that the command
built for each rung of the ladder is the command intended — a remux must not re-encode, and
a re-encode must not hand a hardware encoder frames it cannot take — and that only one
stream can be running at a time.

The rest is refusal: with the beta off, the views must behave as though they did not exist.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.reolink_stamina.const import CONF_BETA_RESTREAM, DOMAIN
from custom_components.reolink_stamina.restream import (
    FORMAT_HLS,
    FORMAT_MP4,
    HLS_INIT,
    HLS_PLAYLIST,
    MAX_HEIGHT,
    MODE_COPY,
    MODE_ENCODE,
    SOFTWARE_ENCODER,
    Encoder,
    RestreamManager,
    _available_encoders,
    async_get_manager,
    async_hls_path,
    async_restream_path,
    build_args,
)

from .conftest import FakeApi, FakeHost

_URL = "http://nvr/cgi-bin/api.cgi?cmd=Playback&token=T"

_VAAPI = Encoder(
    name="h264_vaapi",
    input_args=("-vaapi_device", "/dev/dri/renderD128"),
    output_args=("-b:v", "4M"),
    filters=("format=nv12", "hwupload"),
)


# --------------------------------------------------------------------------- paths


def test_the_path_carries_the_conversion_mode() -> None:
    """The view has to know whether it was asked to repackage or to re-encode."""
    path = async_restream_path("entry", 8, "sub", "file", "20260803093001", "20260803073001", 240)
    assert path.startswith("/api/reolink_stamina/restream/copy/entry/8/sub/")
    assert path.endswith("/240")

    encoded = async_restream_path("e", 0, "main", "f", "s", "p", 0, MODE_ENCODE)
    assert encoded.startswith("/api/reolink_stamina/restream/encode/")


def test_a_negative_seek_never_reaches_the_path() -> None:
    """Nonsense offsets are clamped rather than passed on, as on the flv route."""
    assert async_restream_path("e", 0, "sub", "f", "s", "p", -30).endswith("/0")


def test_the_hls_path_names_only_the_session() -> None:
    """A playlist is addressed by its token, because iOS cannot sign anything."""
    assert async_hls_path("tok") == f"/api/reolink_stamina/hls/tok/{HLS_PLAYLIST}"


# ------------------------------------------------------------------ ffmpeg commands


def test_a_remux_re_encodes_nothing() -> None:
    """The cheap rung: a different container and the same video, bit for bit.

    This is the route an iPhone uses for an H.264 recording, so anything that crept in
    here — a scale filter, an encoder, a hardware device — would turn the lightest option
    into the most expensive one.
    """
    args = build_args("ffmpeg", _URL, mode=MODE_COPY, output_format=FORMAT_MP4)

    assert "-c:v" in args
    assert args[args.index("-c:v") + 1] == "copy"
    assert "-vf" not in args
    assert "-hwaccel" not in args
    assert "libx264" not in args
    # Audio is always converted: Reolink also serves ADPCM and G.711, which MP4 cannot hold.
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[-3:] == ["-f", "mp4", "pipe:1"]
    assert "frag_keyframe+empty_moov+default_base_moof" in args


def test_a_re_encode_scales_down_but_never_up() -> None:
    """A full-sensor stream cannot be re-encoded in real time; it is capped, not resized."""
    args = build_args("ffmpeg", _URL, mode=MODE_ENCODE, output_format=FORMAT_MP4)

    assert args[args.index("-c:v") + 1] == SOFTWARE_ENCODER.name
    # The comma inside min() has to be escaped, or ffmpeg reads it as the next filter.
    assert args[args.index("-vf") + 1] == f"scale=-2:min(ih\\,{MAX_HEIGHT})"
    # Hardware decoding where the machine has it: it is the expensive half of the work.
    assert args[args.index("-hwaccel") + 1] == "auto"


def test_a_hardware_encoder_gets_its_device_before_the_input() -> None:
    """A device is an input option to ffmpeg, and the upload has to follow the scaling."""
    args = build_args("ffmpeg", _URL, mode=MODE_ENCODE, output_format=FORMAT_MP4, encoder=_VAAPI)

    assert args.index("-vaapi_device") < args.index("-i")
    assert args[args.index("-vf") + 1] == f"scale=-2:min(ih\\,{MAX_HEIGHT}),format=nv12,hwupload"
    assert args[args.index("-c:v") + 1] == "h264_vaapi"
    assert "4M" in args


def test_hls_segments_are_fragmented_mp4(tmp_path: Path) -> None:
    """Fragmented MP4 rather than MPEG-TS, because it is what reliably carries H.265.

    That is the whole point of the remux rung on an Apple device: the phone decodes the
    recording as it stands, and TS segments would put that out of reach.
    """
    args = build_args("ffmpeg", _URL, mode=MODE_COPY, output_format=FORMAT_HLS, directory=tmp_path)

    assert args[args.index("-hls_segment_type") + 1] == "fmp4"
    assert args[args.index("-hls_fmp4_init_filename") + 1] == HLS_INIT
    assert args[-1] == str(tmp_path / HLS_PLAYLIST)
    # Copying cannot place keyframes, so it must not claim to.
    assert "-force_key_frames" not in args


def test_a_re_encoded_hls_stream_places_its_own_keyframes(tmp_path: Path) -> None:
    """Segments have to begin on a keyframe, and the recorder's interval is longer."""
    args = build_args(
        "ffmpeg", _URL, mode=MODE_ENCODE, output_format=FORMAT_HLS, directory=tmp_path
    )
    assert args[args.index("-force_key_frames") + 1].startswith("expr:gte(t,n_forced*")


def test_hls_needs_somewhere_to_write() -> None:
    """Refused rather than writing segments into the working directory."""
    with pytest.raises(ValueError):
        build_args("ffmpeg", _URL, mode=MODE_COPY, output_format=FORMAT_HLS)


def test_encoder_listing_reads_video_encoders_only() -> None:
    """`ffmpeg -encoders` also lists audio and subtitle encoders, which are not candidates."""
    listing = """Encoders:
 V..... = Video
 ------
 V....D h264_v4l2m2m         V4L2 mem2mem H.264 encoder
 V..... libx264              libx264 H.264
 A..... aac                  AAC (Advanced Audio Coding)
 S..... srt                  SubRip subtitle
"""
    assert _available_encoders(listing) == {"h264_v4l2m2m", "libx264"}


# ------------------------------------------------------------------------- the slot


class _FakeStream:
    """Stands in for a running ffmpeg, and records that it was stopped."""

    def __init__(self, label: str = "one") -> None:
        self.label = label
        self.stopped = False

    async def async_stop(self) -> None:
        self.stopped = True


async def test_only_one_stream_runs_at_a_time(hass: HomeAssistant) -> None:
    """Whoever starts a stream gets the slot, and whoever had it loses it.

    Seeking reopens the recording at another offset, so a viewer replaces their own stream
    routinely — which is why this evicts rather than refuses.
    """
    manager = RestreamManager(hass)
    first = _FakeStream("first")
    second = _FakeStream("second")

    await manager.async_claim(first)
    await manager.async_claim(second)

    assert first.stopped is True
    assert second.stopped is False


async def test_releasing_a_replaced_stream_does_not_stop_the_live_one(
    hass: HomeAssistant,
) -> None:
    """The evicted request still runs its own cleanup, and must not take over the slot."""
    manager = RestreamManager(hass)
    first = _FakeStream("first")
    second = _FakeStream("second")
    await manager.async_claim(first)
    await manager.async_claim(second)

    await manager.async_release(first)

    assert second.stopped is False
    assert manager.hls_session("anything") is None


async def test_a_broken_hardware_encoder_is_never_chosen_again(hass: HomeAssistant) -> None:
    """A GPU that is present but not working must cost one clip, not every clip."""
    manager = async_get_manager(hass)
    manager.encoder = _VAAPI

    manager.note_encoder_failure(_VAAPI)

    assert _VAAPI.name in manager.failed_encoders
    # Cleared, so the next stream probes again and picks something else.
    assert manager.encoder is None


async def test_software_encoding_failing_is_not_blamed_on_the_encoder(
    hass: HomeAssistant,
) -> None:
    """libx264 is always available; forgetting it would leave nothing to fall back to."""
    manager = async_get_manager(hass)
    manager.encoder = SOFTWARE_ENCODER

    manager.note_encoder_failure(SOFTWARE_ENCODER)

    assert manager.failed_encoders == set()
    assert manager.encoder is SOFTWARE_ENCODER


# --------------------------------------------------------------------------- views


@pytest.fixture
async def setup_stamina(hass: HomeAssistant):
    """Set up Stamina alongside a fake Reolink NVR, with the beta configurable."""

    async def _setup(*, beta: bool):
        assert await async_setup_component(hass, "http", {})

        api = FakeApi(channels=[0])
        reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
        reolink.add_to_hass(hass)
        reolink.runtime_data = SimpleNamespace(host=FakeHost(api))
        reolink.mock_state(hass, ConfigEntryState.LOADED)

        entry = MockConfigEntry(
            domain=DOMAIN, title="Reolink Events", options={CONF_BETA_RESTREAM: beta}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return SimpleNamespace(api=api, reolink=reolink, entry=entry)

    return _setup


async def test_the_restream_view_does_not_exist_while_the_beta_is_off(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """The URL is registered whatever the option says, so refusing is the option."""
    data = await setup_stamina(beta=False)
    client = await hass_client()

    path = async_restream_path(data.reolink.entry_id, 0, "sub", "file", "s", "p", 0)
    response = await client.get(path)

    assert response.status == 404


async def test_the_restream_view_says_so_when_there_is_no_ffmpeg(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """Naming the missing dependency beats an empty video and a log line."""
    data = await setup_stamina(beta=True)
    client = await hass_client()

    with (
        patch(
            "custom_components.reolink_stamina.restream.async_playback_source",
            return_value=_URL,
        ),
        patch(
            "custom_components.reolink_stamina.restream.async_ffmpeg_binary",
            return_value=None,
        ),
    ):
        path = async_restream_path(data.reolink.entry_id, 0, "sub", "file", "s", "p", 0)
        response = await client.get(path)

    assert response.status == 501
    assert "ffmpeg" in await response.text()


async def test_an_unknown_conversion_mode_is_refused(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """The mode is in the path, so it is worth checking rather than trusting."""
    data = await setup_stamina(beta=True)
    client = await hass_client()

    response = await client.get(
        f"/api/reolink_stamina/restream/reencode-everything/{data.reolink.entry_id}"
        "/0/sub/ZmlsZQ==/s/p/0"
    )

    assert response.status == 400


async def test_hls_serves_nothing_but_a_playlist_or_a_segment(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """The session token is the only credential, so what it can reach is bounded.

    Checked before the token is even looked up, because this is the guard that stops a
    session being used to read the rest of the filesystem.
    """
    await setup_stamina(beta=True)
    client = await hass_client()

    response = await client.get("/api/reolink_stamina/hls/token/secrets.yaml")
    assert response.status == 400


async def test_an_expired_hls_session_is_a_plain_404(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """Expected rather than exceptional: the panel reopens when it sees this."""
    await setup_stamina(beta=True)
    client = await hass_client()

    response = await client.get(f"/api/reolink_stamina/hls/nosuchtoken/{HLS_PLAYLIST}")
    assert response.status == 404
