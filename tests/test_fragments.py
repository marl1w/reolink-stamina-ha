"""Tests for clips cut by the recorder itself.

The value of `NvrDownload` is that the panel stops rebuilding containers: the recorder is
asked for a start and an end and hands back an MP4. What has to be right is the request —
the time format the device expects, and the path that reaches its own proxy.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode
import datetime as dt

import pytest

from custom_components.reolink_stamina.fragments import (
    MAX_FRAGMENT_SECONDS,
    VOD_TYPE_NVR_DOWNLOAD,
    async_fragment_path,
    reolink_time_id,
)

TZ = dt.timezone(dt.timedelta(hours=2))


def test_time_id_is_the_format_the_recorder_expects() -> None:
    """reolink_aio slices this string by position, so every field must be padded."""
    assert reolink_time_id(dt.datetime(2026, 8, 4, 8, 45, 12)) == "20260804084512"
    assert reolink_time_id(dt.datetime(2026, 1, 2, 3, 4, 5)) == "20260102030405"


def test_an_aware_time_becomes_local_wall_clock() -> None:
    """The device reads these fields without converting them.

    Sending 08:45 UTC when the recorder means 10:45 local would ask for footage two hours
    from the event, which is the kind of mistake that returns an empty file rather than an
    error.
    """
    aware = dt.datetime(2026, 8, 4, 8, 45, 12, tzinfo=dt.UTC)
    local = aware.astimezone()
    assert reolink_time_id(aware) == reolink_time_id(local.replace(tzinfo=None))


def test_the_path_carries_both_times_and_the_request_type() -> None:
    """The two times travel joined by an underscore, in place of a file name."""
    start = dt.datetime(2026, 8, 4, 8, 45, 12)
    path = async_fragment_path("entry123", 3, "sub", start, start + dt.timedelta(seconds=30))

    assert path.startswith("/api/reolink/video/entry123/3/sub/")
    assert f"/{VOD_TYPE_NVR_DOWNLOAD}/" in path
    encoded = path.rsplit("/", 1)[-1]
    assert urlsafe_b64decode(encoded.encode()).decode() == "20260804084512_20260804084542"


def test_a_backwards_window_is_refused() -> None:
    """Better a clear error here than an empty file from the recorder."""
    start = dt.datetime(2026, 8, 4, 8, 45, 12)
    with pytest.raises(ValueError):
        async_fragment_path("entry", 0, "sub", start, start)
    with pytest.raises(ValueError):
        async_fragment_path("entry", 0, "sub", start, start - dt.timedelta(seconds=10))


def test_an_absurd_window_is_refused() -> None:
    """The recorder assembles the whole fragment before sending a byte of it.

    Asking for an afternoon would simply hang the request, so the ceiling is enforced
    before the device is troubled.
    """
    start = dt.datetime(2026, 8, 4, 8, 45, 12)
    too_long = start + dt.timedelta(seconds=MAX_FRAGMENT_SECONDS + 1)
    with pytest.raises(ValueError, match="at most"):
        async_fragment_path("entry", 0, "sub", start, too_long)

    # Exactly at the ceiling is allowed.
    at_limit = start + dt.timedelta(seconds=MAX_FRAGMENT_SECONDS)
    assert async_fragment_path("entry", 0, "sub", start, at_limit)
