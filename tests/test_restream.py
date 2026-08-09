"""Tests for adaptive playback.

Two things matter here: that the command built for each rung of the ladder is the command
intended — a remux must not re-encode, and a re-encode must not hand a hardware encoder
frames it cannot take — and that only one stream can be running at a time.

Mostly that is asserted without starting ffmpeg, which is cheap and enough for anything
about the *shape* of the command. It is not enough for whether ffmpeg will accept it: a
flag it rejects reads exactly like one it takes, and `-tag:v hvc1` on the piped route was
precisely that — the intended arguments, refused, and the remux rung broken for H.264 in
every non-Apple browser. So the two remux rungs are also run for real against a generated
H.264 clip, and skipped where there is no ffmpeg to run them with.

The rest is refusal: a malformed request must be turned away by the views rather than
handed to ffmpeg.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.reolink_stamina.const import DOMAIN
from custom_components.reolink_stamina.restream import (
    FORMAT_HLS,
    FORMAT_MP4,
    HLS_IDLE_TIMEOUT,
    HLS_INIT,
    HLS_MAX_SESSION_SECONDS,
    HLS_PLAYLIST,
    MAX_HEIGHT,
    MODE_COPY,
    MODE_ENCODE,
    SESSION_PREFIX,
    SOFTWARE_ENCODER,
    Diagnosis,
    Encoder,
    RestreamManager,
    _available_encoders,
    _classify_ffmpeg_error,
    _cpu_load,
    _devices_requested,
    _diagnose_no_output,
    _HlsStream,
    _without_hwaccel_probe,
    async_choose_encoder,
    async_get_manager,
    async_hls_path,
    async_restream_path,
    async_sweep_sessions,
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


def test_audio_timestamps_are_made_monotonic_whichever_rung_this_is() -> None:
    """A seek part-way into a recording arrives with audio timestamps that step backwards.

    The AAC encoder says so — "Queue input is backward in time" — and then encodes them
    anyway, against a clock the video no longer shares. It applies to both rungs, because
    audio is converted on both, and it must not shift where the audio starts: on the copied
    rung the video keeps the recorder's own timestamps and nothing would realign them.
    """
    for mode in (MODE_COPY, MODE_ENCODE):
        args = build_args("ffmpeg", _URL, mode=mode, output_format=FORMAT_MP4)

        assert args[args.index("-af") + 1] == "aresample=async=1"
        assert args.index("-af") < args.index("-c:a")


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


def test_a_copied_hls_stream_is_tagged_so_safari_will_take_it(tmp_path: Path) -> None:
    """`hev1` is what ffmpeg writes when the source carried no tag, and Safari refuses it.

    The recorder's FLV never carries one, so without this the rung that exists to let a device
    use its own decoder produces segments that device will not open — and the only way past it
    is the re-encode this was supposed to avoid.
    """
    args = build_args("ffmpeg", _URL, mode=MODE_COPY, output_format=FORMAT_HLS, directory=tmp_path)

    assert args[args.index("-tag:v") + 1] == "hvc1"
    assert args.index("-tag:v") < args.index("-f")
    # Re-encoding produces H.264, where the tag would mean nothing.
    assert "-tag:v" not in build_args(
        "ffmpeg", _URL, mode=MODE_ENCODE, output_format=FORMAT_HLS, directory=tmp_path
    )


def test_the_piped_route_asks_for_no_tag_at_all() -> None:
    """The plain MP4 muxer refuses a tag it cannot apply, where the segmenter ignores one.

    `-tag:v hvc1` against an H.264 stream is "Tag hvc1 incompatible with output codec id
    '27'", and ffmpeg then writes no header — so asking for it here broke every H.264 remux,
    which is the common case and the route Chrome and Firefox use. Nothing on this route
    wants it: preferring `hvc1` over `hev1` is a Safari quirk, and Safari is served HLS.
    """
    assert "-tag:v" not in build_args("ffmpeg", _URL, mode=MODE_COPY, output_format=FORMAT_MP4)


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


# ------------------------------------------------------- the command actually running

_FFMPEG = shutil.which("ffmpeg")
_needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="needs a real ffmpeg to run")


def _h264_sample(directory: Path) -> str:
    """Two seconds of H.264 and AAC in MP4, standing in for an H.264 recording."""
    path = directory / "h264.mp4"
    subprocess.run(
        [
            _FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=d=2:s=320x240",
            "-f", "lavfi", "-i", "sine=d=2",
            "-c:v", "libx264", "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    return str(path)


@_needs_ffmpeg
def test_an_h264_recording_survives_the_piped_remux(tmp_path: Path) -> None:
    """The regression that asserting the argument list could never have caught.

    Every check above reads the command and stops there, so a flag that ffmpeg itself
    rejects looks identical to one it accepts. `-tag:v hvc1` on this route was exactly
    that: the arguments were the intended ones and ffmpeg refused them, and the remux rung
    was broken for H.264 — the ordinary case — in every browser that is not an Apple one.

    So this one runs the binary. H.264 is what matters: HEVC was never the failing half.
    """
    args = build_args(_FFMPEG, _h264_sample(tmp_path), mode=MODE_COPY, output_format=FORMAT_MP4)

    done = subprocess.run(args, capture_output=True, timeout=60)

    assert done.returncode == 0, done.stderr.decode(errors="replace")
    # A header and at least one fragment, rather than the nothing a refused tag leaves.
    assert len(done.stdout) > 1024


@_needs_ffmpeg
def test_an_h264_recording_survives_the_hls_remux(tmp_path: Path) -> None:
    """And the tag the other rung does ask for is one this rung's muxer merely ignores.

    Which is why the mistake survived: the segmenter writes `avc1` for an H.264 stream and
    says nothing, so the tag really is free here — it is only the piped MP4 muxer that
    refuses it. Asserted rather than assumed, because the whole bug was that assumption.
    """
    segments = tmp_path / "out"
    segments.mkdir()
    args = build_args(
        _FFMPEG, _h264_sample(tmp_path), mode=MODE_COPY, output_format=FORMAT_HLS,
        directory=segments,
    )  # fmt: skip

    done = subprocess.run(args, capture_output=True, timeout=60)

    assert done.returncode == 0, done.stderr.decode(errors="replace")
    assert (segments / HLS_PLAYLIST).is_file()
    assert b"avc1" in (segments / HLS_INIT).read_bytes()


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


async def test_an_encoder_that_is_listed_but_cannot_run_is_not_chosen(
    hass: HomeAssistant,
) -> None:
    """The listing says what ffmpeg was built with, which is not what the machine can do.

    The case this exists for, seen in the field: a virtual machine with a graphics device that
    has a render node and no media engine. Every hardware encoder is listed, `/dev/dri` is
    there, and not one of them can encode a frame.
    """
    with (
        patch(
            "custom_components.reolink_stamina.restream.encoders._available_encoders",
            return_value={"h264_qsv", "h264_vaapi", "libx264"},
        ),
        patch("custom_components.reolink_stamina.restream.encoders.Path.exists", return_value=True),
        patch(
            "custom_components.reolink_stamina.restream.encoders._async_encoder_works",
            return_value=False,
        ) as tested,
        patch("asyncio.create_subprocess_exec", side_effect=_listing_process),
    ):
        chosen = await async_choose_encoder(hass, "ffmpeg")

    assert chosen is SOFTWARE_ENCODER
    # Each was tried rather than assumed, and each is remembered so it is not tried again.
    assert tested.call_count == 2
    assert async_get_manager(hass).failed_encoders == {"h264_qsv", "h264_vaapi"}


async def test_a_working_hardware_encoder_is_chosen_and_the_rest_left_alone(
    hass: HomeAssistant,
) -> None:
    """Testing stops at the first that works: the probe is startup cost, so it stays small."""
    with (
        patch(
            "custom_components.reolink_stamina.restream.encoders._available_encoders",
            return_value={"h264_qsv", "h264_vaapi", "libx264"},
        ),
        patch("custom_components.reolink_stamina.restream.encoders.Path.exists", return_value=True),
        patch(
            "custom_components.reolink_stamina.restream.encoders._async_encoder_works",
            return_value=True,
        ) as tested,
        patch("asyncio.create_subprocess_exec", side_effect=_listing_process),
    ):
        chosen = await async_choose_encoder(hass, "ffmpeg")

    assert chosen.name == "h264_qsv"
    assert tested.call_count == 1
    assert async_get_manager(hass).failed_encoders == set()


async def _listing_process(*args: str, **kwargs: object) -> SimpleNamespace:
    """Stand in for `ffmpeg -encoders`, whose output the test patches out anyway."""

    async def communicate() -> tuple[bytes, bytes]:
        return b"", b""

    return SimpleNamespace(communicate=communicate, returncode=0)


# ------------------------------------------------- what ffmpeg said, and what it meant


# Verbatim from a Home Assistant OS installation whose graphics device could not be used:
# three device types `-hwaccel auto` tried on the way past, none of them asked for, none of
# them fatal, and between them longer than the extract that used to be classified.
_HWACCEL_NOISE = (
    "[VAAPI @ 0x7f3864eb8ac0] Failed to initialise VAAPI connection: -1 (unknown libva error).\n"
    "Device creation failed: -5.\n"
    "[VDPAU @ 0x7f3864eb8ac0] Cannot open the X11 display .\n"
    "Device creation failed: -1313558101.\n"
    "[Vulkan @ 0x7f3864eb8ac0] Instance creation failure: VK_ERROR_INCOMPATIBLE_DRIVER\n"
    "Device creation failed: -40.\n"
)


def test_the_hardware_probe_is_not_read_as_a_failure() -> None:
    """`-hwaccel auto` fails loudly, non-fatally, and first. It must not be read at all.

    The bug this is here for: on a machine with no working acceleration these lines are the
    only thing that ever fitted in the extract, so *every* re-encode that produced nothing was
    diagnosed as a broken encoder — and each diagnosis retired one, until there were none.
    """
    kept = _without_hwaccel_probe(_HWACCEL_NOISE + "recorder is slow", requested=frozenset())

    assert kept == "recorder is slow"
    assert _classify_ffmpeg_error(kept) is None


def test_the_device_the_encoder_asked_for_is_evidence_not_noise() -> None:
    """VAAPI failing to open the device it was handed is the reason the clip did not play."""
    said = (
        "[VAAPI @ 0x7fc266b24d40] Failed to initialise VAAPI connection: -1 (unknown libva error)."
        "\nDevice creation failed: -5.\n"
        "Failed to set value '/dev/dri/renderD128' for option 'vaapi_device': I/O error\n"
        "Error parsing global options: I/O error"
    )

    kept = _without_hwaccel_probe(said, requested=_devices_requested(_VAAPI))

    assert kept == said
    diagnosis = _classify_ffmpeg_error(kept)
    assert diagnosis is not None
    assert diagnosis.code == "encoder_unavailable"
    assert diagnosis.encoder_at_fault is True


def test_a_slow_clip_behind_the_probe_is_still_a_slow_clip() -> None:
    """The whole point: the same noise, and a diagnosis that now depends on what follows it."""
    stream = _stalled(stderr="")
    stream.error_text = _without_hwaccel_probe(_HWACCEL_NOISE, requested=frozenset())

    diagnosis = _diagnose_no_output(
        stream, elapsed=30.0, load=3.1, opened=True, progress="0 of the 2 segments"
    )

    assert diagnosis.code == "machine_too_slow"
    # And so the machine keeps whatever hardware encoder it had.
    assert diagnosis.encoder_at_fault is False


def test_the_encoders_own_complaint_survives_the_filter() -> None:
    """Only the device probe goes. What the encoder itself said is the whole diagnosis."""
    said = _HWACCEL_NOISE + "[h264_qsv @ 0x55d3] Error initializing an internal MFX session"

    diagnosis = _classify_ffmpeg_error(_without_hwaccel_probe(said, requested=frozenset()))

    assert diagnosis is not None
    assert diagnosis.code == "encoder_unavailable"


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


# ------------------------------------------------------------------ session cleanup


class _FakeProcess:
    """A running ffmpeg that can be killed, and notices that it was."""

    def __init__(self) -> None:
        self.returncode = None
        self.stderr = None
        self.pid = 1
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        # Suspends, which is where a cancellation aimed at the current task lands — and so
        # is exactly the await that used to swallow the rest of the teardown.
        await asyncio.sleep(0.01)
        self.returncode = -9
        return self.returncode


async def test_a_session_nobody_is_reading_deletes_its_own_files(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The ordinary end of a session: the panel was closed and nothing read a segment again.

    The watchdog stops the session it belongs to, so stopping used to cancel the very task
    it was running in — killing ffmpeg, then raising `CancelledError` at the next await and
    never reaching the removal. Every abandoned session then left its directory behind in
    the temporary filesystem, which on most installations is memory, until it filled and
    every conversion after that failed for want of space.
    """
    directory = tmp_path / "session"
    directory.mkdir()
    (directory / "s00000.m4s").write_bytes(b"segment")
    process = _FakeProcess()

    with patch("custom_components.reolink_stamina.restream.sessions._HLS_SWEEP_INTERVAL", 0.01):
        session = _HlsStream(hass, process, "label", SOFTWARE_ENCODER, "token", directory)
        # Nobody has asked for a segment since well before the idle timeout.
        session.last_read = time.monotonic() - HLS_IDLE_TIMEOUT - 1
        await asyncio.sleep(0.3)

    assert process.killed is True
    assert not directory.exists()


async def test_a_session_stopped_from_outside_still_stops_its_watchdog(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The other half of the same decision: a watchdog cancelled by someone else must be.

    Left running it would stop a session that has already gone, so this is not merely tidy.
    """
    directory = tmp_path / "session"
    directory.mkdir()

    session = _HlsStream(hass, _FakeProcess(), "label", SOFTWARE_ENCODER, "token", directory)
    await async_get_manager(hass).async_release(session)

    assert session._watchdog.cancelled() or session._watchdog.done()
    assert not directory.exists()


async def test_setup_reclaims_what_an_earlier_run_left_behind(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Restarting Home Assistant does not empty the temporary filesystem, so this does.

    Everything here was leaked by a version whose teardown skipped the removal, and no
    restart gives the space back: depending on how the filesystem is mounted it survives
    until the machine reboots, or indefinitely.
    """
    stale = tmp_path / f"{SESSION_PREFIX}old"
    stale.mkdir()
    (stale / "s00000.m4s").write_bytes(b"segment")
    os.utime(stale, (time.time() - HLS_MAX_SESSION_SECONDS - 60,) * 2)

    with patch(
        "custom_components.reolink_stamina.restream.sessions.tempfile.gettempdir",
        return_value=str(tmp_path),
    ):
        removed = await async_sweep_sessions(hass)

    assert removed == 1
    assert not stale.exists()


async def test_the_sweep_leaves_a_session_that_is_still_playing(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """This also runs on a reload, and a reload does not stop a session someone is watching.

    Age is what separates the two: the watchdog stops any session at the maximum age, so
    nothing legitimate is older, and a live session's directory is touched continuously as
    segments are written and rotated out.
    """
    live = tmp_path / f"{SESSION_PREFIX}live"
    live.mkdir()
    (live / "s00000.m4s").write_bytes(b"segment")

    with patch(
        "custom_components.reolink_stamina.restream.sessions.tempfile.gettempdir",
        return_value=str(tmp_path),
    ):
        removed = await async_sweep_sessions(hass)

    assert removed == 0
    assert live.exists()


async def test_the_sweep_touches_nothing_it_did_not_write(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """It runs over a shared temporary directory, so the name is the whole guard."""
    someone_else = tmp_path / "important_backup"
    someone_else.mkdir()
    os.utime(someone_else, (time.time() - HLS_MAX_SESSION_SECONDS - 60,) * 2)

    with patch(
        "custom_components.reolink_stamina.restream.sessions.tempfile.gettempdir",
        return_value=str(tmp_path),
    ):
        removed = await async_sweep_sessions(hass)

    assert removed == 0
    assert someone_else.exists()


# ---------------------------------------------------------------------- diagnosis


def _stalled(*, encoder: Encoder = SOFTWARE_ENCODER, stderr: str = "", exited: bool = False):
    """Return a stream that produced nothing, standing in for a real one."""
    return SimpleNamespace(
        label="entry/0/main@0s hls",
        encoder=encoder,
        # The real stream classifies all of what ffmpeg said and quotes only the head of it.
        error_text=stderr,
        error_detail=stderr,
        process=SimpleNamespace(returncode=1 if exited else None, pid=1),
    )


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("Connection refused", "device_unreachable"),
        ("Server returned 401 Unauthorized", "device_rejected"),
        ("av_interleaved_write_frame(): No space left on device", "no_space"),
        ("Unknown encoder 'h264_qsv'", "encoder_unavailable"),
        ("Invalid data found when processing input", "unreadable_stream"),
        ("Connection reset by peer", "device_stopped"),
    ],
)
def test_ffmpeg_is_quoted_back_as_a_cause_not_as_output(stderr: str, code: str) -> None:
    """The panel gets a sentence about the recorder or the machine, not ffmpeg's wording."""
    diagnosis = _classify_ffmpeg_error(stderr)

    assert diagnosis is not None
    assert diagnosis.code == code
    # Whatever ffmpeg said, what comes out is a sentence someone can act on.
    assert diagnosis.message.endswith(".")
    assert stderr not in diagnosis.message


def test_only_an_encoder_fault_is_blamed_on_the_encoder() -> None:
    """The distinction the encoder blacklist depends on, asserted where it is decided.

    Blaming every failure on the encoder is how one slow recorder permanently costs a
    machine its working GPU, which is the bug this separation exists to prevent.
    """
    assert _classify_ffmpeg_error("Device creation failed").encoder_at_fault is True
    assert _classify_ffmpeg_error("Connection timed out").encoder_at_fault is False
    assert _classify_ffmpeg_error("") is None


def test_a_converter_using_the_processor_blames_the_machine() -> None:
    """Work is being done and it is not fast enough — which is the machine's problem."""
    diagnosis = _diagnose_no_output(
        _stalled(), elapsed=30.0, load=2.4, opened=True, progress="0 of the 2 segments"
    )

    assert diagnosis.code == "machine_too_slow"
    assert "2.4 processor cores" in diagnosis.message
    # Falling back from hardware to software would only make a slow machine slower.
    assert diagnosis.encoder_at_fault is False


def test_an_idle_converter_blames_the_recorder() -> None:
    """Nothing is being computed, so nothing is arriving to compute."""
    diagnosis = _diagnose_no_output(
        _stalled(), elapsed=30.0, load=0.01, opened=True, progress="1 of the 2 segments"
    )

    assert diagnosis.code == "device_too_slow"
    assert "recorder" in diagnosis.message


def test_an_unmeasurable_load_says_less_rather_than_guessing() -> None:
    """Where processor time cannot be read, the diagnosis stops short of naming a culprit."""
    diagnosis = _diagnose_no_output(
        _stalled(), elapsed=30.0, load=None, opened=True, progress="0 of the 2 segments"
    )

    assert diagnosis.code == "too_slow"
    assert "machine" not in diagnosis.message


def test_a_recorder_that_never_sent_a_header_is_named_as_such() -> None:
    """The header is written as soon as ffmpeg knows the codec, so its absence is the tell."""
    diagnosis = _diagnose_no_output(
        _stalled(), elapsed=30.0, load=0.0, opened=False, progress="no segments"
    )

    assert diagnosis.code == "device_sent_nothing"


def test_a_hardware_encoder_dying_silently_is_still_the_encoder(hass: HomeAssistant) -> None:
    """The fallback that makes the next attempt work has to survive the new classification.

    A hardware encoder that exits immediately and says nothing recognisable is much the
    likeliest cause, and is the one case worth never retrying.
    """
    diagnosis = _diagnose_no_output(
        _stalled(encoder=_VAAPI, exited=True), elapsed=0.4, load=None, opened=False, progress="none"
    )

    assert diagnosis.code == "stopped_early"
    assert diagnosis.encoder_at_fault is True


def test_software_dying_silently_is_not_the_encoder() -> None:
    """libx264 always exists, so its failure is about the input, not about the encoder."""
    diagnosis = _diagnose_no_output(
        _stalled(exited=True), elapsed=0.4, load=None, opened=False, progress="none"
    )

    assert diagnosis.encoder_at_fault is False


def test_load_is_not_invented_from_a_missing_reading() -> None:
    """Processor time is unreadable off Linux, and a wrong culprit is worse than none."""
    assert _cpu_load(None, 4.0, 10.0) is None
    assert _cpu_load(1.0, None, 10.0) is None
    assert _cpu_load(1.0, 4.0, 0.0) is None
    assert _cpu_load(1.0, 4.0, 10.0) == pytest.approx(0.3)


async def test_a_failure_is_recorded_for_the_panel_to_read(hass: HomeAssistant) -> None:
    """The 502 body reaches nobody, so the reason has to be fetchable afterwards."""
    manager = RestreamManager(hass)
    stream = _stalled(encoder=_VAAPI, stderr="Device creation failed")

    broken = Diagnosis("encoder_unavailable", "GPU broke.", True)

    manager.note_failure(stream, broken, mode="encode")

    assert len(manager.failures) == 1
    recorded = manager.failures[-1]
    assert recorded["code"] == "encoder_unavailable"
    assert recorded["encoder"] == _VAAPI.name
    # ffmpeg's own words are kept for the diagnostics download, not for the panel.
    assert recorded["ffmpeg"] == "Device creation failed"
    # And the diagnosis named the encoder, so it is not chosen again.
    assert _VAAPI.name in manager.failed_encoders


async def test_a_slow_machine_does_not_cost_the_gpu(hass: HomeAssistant) -> None:
    """The bug this whole separation exists for: one slow clip disabling working hardware."""
    manager = RestreamManager(hass)
    manager.encoder = _VAAPI

    manager.note_failure(
        _stalled(encoder=_VAAPI),
        Diagnosis("machine_too_slow", "Too slow.", False),
        mode="encode",
    )

    assert manager.failed_encoders == set()
    assert manager.encoder is _VAAPI


async def test_only_the_last_few_failures_are_kept(hass: HomeAssistant) -> None:
    """Enough to show a pattern, without a session's worth of noise."""
    manager = RestreamManager(hass)
    for index in range(25):
        slow = Diagnosis("too_slow", f"Attempt {index}.", False)
        manager.note_failure(_stalled(), slow, mode="copy")

    assert len(manager.failures) == 10
    assert manager.failures[-1]["message"] == "Attempt 24."


# --------------------------------------------------------------------------- views


@pytest.fixture
async def setup_stamina(hass: HomeAssistant):
    """Set up Stamina alongside a fake Reolink NVR."""
    assert await async_setup_component(hass, "http", {})

    api = FakeApi(channels=[0])
    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(api))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    entry = MockConfigEntry(domain=DOMAIN, title="Reolink Events")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return SimpleNamespace(api=api, reolink=reolink, entry=entry)


async def test_the_restream_view_says_so_when_there_is_no_ffmpeg(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """Naming the missing dependency beats an empty video and a log line."""
    data = setup_stamina
    client = await hass_client()

    with (
        patch(
            "custom_components.reolink_stamina.restream.views.async_playback_source",
            return_value=_URL,
        ),
        patch(
            "custom_components.reolink_stamina.restream.runner.async_ffmpeg_binary",
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
    data = setup_stamina
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
    client = await hass_client()

    response = await client.get("/api/reolink_stamina/hls/token/secrets.yaml")
    assert response.status == 400


async def test_an_expired_hls_session_is_a_plain_404(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, setup_stamina
) -> None:
    """Expected rather than exceptional: the panel reopens when it sees this."""
    client = await hass_client()

    response = await client.get(f"/api/reolink_stamina/hls/nosuchtoken/{HLS_PLAYLIST}")
    assert response.status == 404
