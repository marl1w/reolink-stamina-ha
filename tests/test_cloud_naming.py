"""Tests for where a clip lands in the cloud."""

from __future__ import annotations

import datetime as dt

from custom_components.reolink_stamina.cloud.naming import (
    clip_filename,
    remote_path,
    safe_part,
)


def test_a_clip_is_named_after_when_what_and_which() -> None:
    """Date first, so an alphabetical listing is chronological."""
    when = dt.datetime(2026, 8, 4, 8, 45, 12)
    assert clip_filename(when, "main-nvr", "South Side") == (
        "260804_084512_main-nvr_South Side.mp4"
    )


def test_the_time_carries_no_colons() -> None:
    """OneDrive, SharePoint and Windows all refuse them.

    The obvious HH:MM:SS would be rejected at upload time, after the clip had been fetched.
    """
    name = clip_filename(dt.datetime(2026, 8, 4, 8, 45, 12), "nvr", "cam")
    assert ":" not in name


def test_forbidden_characters_are_replaced_not_dropped() -> None:
    """A camera called "Front / Gate" must still produce a legible name."""
    assert safe_part("Front / Gate") == "Front - Gate"
    assert safe_part('bad:*?"<>|chars') == "bad-chars"
    assert safe_part("trailing dot.") == "trailing dot"
    assert safe_part("  padded  ") == "padded"


def test_a_name_that_sanitises_to_nothing_still_has_one() -> None:
    """Better a placeholder than an upload to a path ending in a slash."""
    assert safe_part("///", fallback="camera") == "camera"
    assert clip_filename(dt.datetime(2026, 8, 4, 8, 45, 12), "::", "//").endswith("_nvr_camera.mp4")


def test_the_path_is_folder_then_file() -> None:
    """What the user asked to see in their drive."""
    name = clip_filename(dt.datetime(2026, 8, 4, 8, 45, 12), "main-nvr", "South Side")
    assert remote_path("Reolink/Main NVR", name) == (
        "Reolink/Main NVR/260804_084512_main-nvr_South Side.mp4"
    )


def test_a_folder_is_sanitised_a_segment_at_a_time() -> None:
    """The folder is the recorder's name under a root, and either can hold anything."""
    assert remote_path("Reolink/Front: Gate", "clip.mp4") == ("Reolink/Front- Gate/clip.mp4")


def test_a_folder_cannot_escape_itself() -> None:
    """A folder of "../.." must not write outside where the user pointed us."""
    segments = remote_path("../../etc", "clip.mp4").split("/")

    assert segments == ["etc", "clip.mp4"]


def test_a_folder_of_nothing_still_lands_somewhere_sensible() -> None:
    """An empty folder must not produce a path starting with a slash."""
    assert remote_path("   ", "clip.mp4") == "Reolink/clip.mp4"


def test_an_unusual_clip_says_so_in_its_name() -> None:
    """The handful worth looking at have to be visible in a plain file listing.

    In the name rather than a folder of its own, so it survives somebody moving the file and
    can be searched for in any cloud client.
    """
    import datetime as dt

    from custom_components.reolink_stamina.cloud.naming import clip_filename

    when = dt.datetime(2026, 8, 4, 3, 11, 2)

    assert clip_filename(when, "Main NVR", "Drive") == "260804_031102_Main NVR_Drive.mp4"
    assert (
        clip_filename(when, "Main NVR", "Drive", unusual=True)
        == "260804_031102_Main NVR_Drive_u.mp4"
    )


def test_a_growing_recording_is_only_complete_when_it_stops() -> None:
    """An event camera extends its recording while it still sees something.

    Fetching at the first sighting would upload the first seven seconds of a two-minute
    event, so the clip waits for two searches to agree on where it ends.
    """
    import datetime as dt

    from custom_components.reolink_stamina.cloud.fetch import stable

    first = dt.datetime(2026, 8, 4, 19, 50, 9)
    grown = dt.datetime(2026, 8, 4, 19, 50, 33)

    assert stable(None, first) is False, "one observation is never enough"
    assert stable(first, grown) is False, "still growing"
    assert stable(grown, grown) is True, "two searches agree"
