"""Contract tests against the real Reolink integration and reolink_aio.

Unlike the rest of the suite, these do not use fakes — they assert against the actual
upstream code installed alongside Home Assistant. They are the tripwire for this
project's one dependency on non-public API: the panel reaches into `entry.runtime_data`,
and calls reolink_aio directly.

If one of these fails after a Home Assistant update, the panel needs updating too, and
the failure message says exactly what moved.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.reolink_stamina.nvr_registry import _REQUIRED_API_ATTRS

# The exact reolink_aio version this integration is developed and tested against.
# Home Assistant 2026.7.4 pins this version.
SUPPORTED_REOLINK_AIO = "0.21.4"


def test_reolink_runtime_data_still_carries_a_host() -> None:
    """`entry.runtime_data.host` is how we reach the live, authenticated API."""
    from homeassistant.components.reolink import ReolinkData

    fields = {field.name for field in dataclasses.fields(ReolinkData)}
    assert "host" in fields, (
        "The Reolink integration's runtime data no longer exposes `host`; "
        "update nvr_registry.async_get_host"
    )


def test_reolink_host_still_exposes_api() -> None:
    """`host.api` is the reolink_aio object every search goes through."""
    from homeassistant.components.reolink.host import ReolinkHost

    assert hasattr(ReolinkHost, "api") or "api" in getattr(ReolinkHost, "__annotations__", {}), (
        "ReolinkHost no longer exposes `api`"
    )


def test_every_api_attribute_we_depend_on_exists() -> None:
    """The capability check in the adapter must match reality."""
    from reolink_aio.api import Host

    missing = [attr for attr in _REQUIRED_API_ATTRS if not hasattr(Host, attr)]
    assert not missing, f"reolink_aio no longer provides: {', '.join(missing)}"


def test_request_vod_files_still_accepts_the_arguments_we_pass() -> None:
    """Signature drift here would break both search and the calendar."""
    import inspect

    from reolink_aio.api import Host

    parameters = inspect.signature(Host.request_vod_files).parameters
    for name in ("channel", "start", "end", "status_only", "stream", "split_time"):
        assert name in parameters, f"request_vod_files lost the '{name}' argument"


def test_vod_file_still_exposes_the_fields_we_serialise() -> None:
    """serialize_file() reads each of these off a VOD_file."""
    from reolink_aio.typings import VOD_file

    for attr in (
        "start_time",
        "end_time",
        "start_time_id",
        "end_time_id",
        "duration",
        "file_name",
        "size",
        "type",
        "triggers",
    ):
        assert isinstance(getattr(VOD_file, attr, None), property), (
            f"VOD_file.{attr} is no longer a property"
        )


def test_vod_search_status_still_exposes_days() -> None:
    """The recording calendar is built from this bitmap."""
    from reolink_aio.typings import VOD_search_status

    for attr in ("year", "month", "days"):
        assert isinstance(getattr(VOD_search_status, attr, None), property)


def test_the_triggers_we_present_still_exist() -> None:
    """Every trigger the panel has a label for must still be a real member.

    The reverse direction is covered in test_vod.py, which asserts that *all* upstream
    members serialise, so a newly added trigger cannot go unnoticed.
    """
    from reolink_aio.typings import VOD_trigger

    presented = {
        "person",
        "vehicle",
        "animal",
        "face",
        "doorbell",
        "package",
        "crying",
        "crossline",
        "intrusion",
        "linger",
        "forgotten_item",
        "taken_item",
        "io",
        "motion",
        "timer",
    }
    members = {member.name.lower() for member in VOD_trigger if member.name}
    assert presented <= members, f"Unknown triggers presented: {presented - members}"


def test_vod_request_types_we_use_still_exist() -> None:
    """The proxy URL encodes this value, and the view parses it back."""
    from reolink_aio.enums import VodRequestType

    assert VodRequestType.DOWNLOAD.value == "Download"
    assert VodRequestType.PLAYBACK.value == "Playback"
    assert VodRequestType.NVR_DOWNLOAD.value == "NvrDownload"


def test_playback_proxy_url_helper_still_exists() -> None:
    """Playback reuses the Reolink integration's own authenticated proxy."""
    import inspect

    from homeassistant.components.reolink.views import (
        PlaybackProxyView,
        async_generate_playback_proxy_url,
    )

    parameters = inspect.signature(async_generate_playback_proxy_url).parameters
    assert list(parameters) == [
        "config_entry_id",
        "channel",
        "filename",
        "stream_res",
        "vod_type",
    ]
    # The panel signs the URL precisely because the view demands authentication.
    assert PlaybackProxyView.requires_auth is True


def test_the_proxy_url_shape_matches_what_we_assert_elsewhere() -> None:
    """Keeps the websocket test's expected prefix honest."""
    from homeassistant.components.reolink.views import async_generate_playback_proxy_url

    url = async_generate_playback_proxy_url("entry", 0, "file.mp4", "sub", "Download")
    assert url.startswith("/api/reolink/video/")


def test_nvr_download_still_takes_a_start_and_an_end() -> None:
    """Clip downloads rest entirely on this.

    `NvrDownload` is what lets the recorder cut a clip itself, and the two times reach it
    through the `filename` argument joined by an underscore — reolink_aio splits them back
    apart. If that convention changes, fragment requests would ask for a file by that
    literal name and fail, so it is pinned here rather than discovered in the field.
    """
    import inspect

    from reolink_aio.api import Host

    helper = getattr(Host, "_generate_NVR_download_vod", None)
    assert helper is not None, "reolink_aio no longer builds NvrDownload requests"
    assert list(inspect.signature(helper).parameters) == [
        "self",
        "start_time",
        "end_time",
        "channel",
        "stream",
    ]
    # The split of `filename` into those two times happens in get_vod_source.
    source = inspect.getsource(Host.get_vod_source)
    assert 'filename.split("_", 1)' in source, "the start_end filename convention moved"


def test_media_source_still_splits_on_the_same_interval() -> None:
    """Our default segment length deliberately matches upstream's.

    Skipped where the `camera` component's native dependencies are unavailable, since
    importing the Reolink media source pulls them in.
    """
    try:
        from homeassistant.components.reolink.media_source import VOD_SPLIT_TIME
    except ImportError as err:
        pytest.skip(f"Reolink media source not importable here: {err}")

    from custom_components.reolink_stamina.const import DEFAULT_SPLIT_MINUTES

    assert VOD_SPLIT_TIME.total_seconds() / 60 == DEFAULT_SPLIT_MINUTES


def test_every_reolink_detection_sensor_is_classified() -> None:
    """Every binary sensor Reolink ships must map to a trigger or be listed as not one.

    This is the test that was missing. The kind lookup used to read only the last
    `_`-separated segment of the unique id, so `crossline_dog_cat` resolved to "cat" and
    three smart-detection animal sensors were dropped without a word — no timeline
    markers, and no cloud sync for animals on cameras using detection zones.

    Reolink adds sensors regularly, and each new one either belongs in `_SENSOR_KINDS` or
    in `_NOT_DETECTIONS`. Failing here is the prompt to decide which.
    """
    from homeassistant.components.reolink import binary_sensor as upstream

    from custom_components.reolink_stamina.detections import _NOT_DETECTIONS, _kind_for_key

    descriptions = [
        *upstream.BINARY_PUSH_SENSORS,
        *upstream.BINARY_SENSORS,
        *upstream.BINARY_SMART_AI_SENSORS,
        upstream.BINARY_IO_INPUT_SENSOR,
    ]
    unclassified = sorted(
        description.key
        for description in descriptions
        if _kind_for_key(description.key.lower()) is None
        and description.key.lower() not in _NOT_DETECTIONS
    )

    assert not unclassified, (
        f"Reolink ships binary sensors this panel silently ignores: {unclassified}. "
        "Add each to _SENSOR_KINDS or to _NOT_DETECTIONS in detections.py."
    )


def test_the_smart_detection_sensors_report_their_subject() -> None:
    """A person crossing a line is a person, and an animal is an animal.

    Pinned explicitly because the mapping is a judgement rather than a lookup: the zone
    is Reolink's concern, the subject is what the timeline shows and what cloud sync's
    per-kind ticks filter on.
    """
    from custom_components.reolink_stamina.detections import _kind_for_key

    for zone in ("crossline", "intrusion", "linger"):
        assert _kind_for_key(f"{zone}_person") == "person"
        assert _kind_for_key(f"{zone}_vehicle") == "vehicle"
        assert _kind_for_key(f"{zone}_dog_cat") == "animal"


def test_installed_reolink_aio_is_the_version_we_pin() -> None:
    """Tests must run against the reolink_aio version this integration is pinned to.

    Home Assistant pins reolink_aio to one exact version per release, and installing
    anything newer lets code depend on attributes real installs do not have. That is
    exactly the 0.1.0 regression: `api.is_dual_lens` exists in 0.21.8 but not in 0.21.4,
    which is what Home Assistant 2026.7.4 ships — so an unpinned `pip install
    reolink-aio` produced a green suite and a broken panel.

    When your Home Assistant bumps the library, bump SUPPORTED_REOLINK_AIO and
    requirements-test.txt together, then re-run the suite.
    """
    import importlib.metadata as metadata

    installed = metadata.version("reolink-aio")
    assert installed == SUPPORTED_REOLINK_AIO, (
        f"Tests expect reolink-aio=={SUPPORTED_REOLINK_AIO} but {installed} is "
        f"installed. Fix with: pip install reolink-aio=={SUPPORTED_REOLINK_AIO}"
    )
