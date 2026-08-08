"""Tests for streaming a recording straight through to the browser.

How the endpoint is chosen and addressed lives in test_playback_route.py; what is left
here is the path the panel is handed.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode

from custom_components.reolink_stamina.flv_proxy import async_flv_path


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
