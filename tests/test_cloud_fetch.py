"""Tests for choosing how to get a clip out of the recorder."""

from __future__ import annotations

from custom_components.reolink_stamina.cloud.fetch import (
    WHOLE_FILE_SLACK_SECONDS,
    wants_whole_file,
)


def test_an_event_recording_is_uploaded_whole() -> None:
    """The camera already made the clip; cutting it would gain nothing.

    Measured on real hardware: a detection produced a 7s recording that grew to about 100s
    while the event continued, starting 8s before the detection.
    """
    assert wants_whole_file(recording_seconds=31, clip_seconds=34) is True
    assert wants_whole_file(recording_seconds=101, clip_seconds=90) is True


def test_a_segment_of_continuous_footage_is_cut() -> None:
    """A half-hour segment must not be uploaded to keep twenty seconds of it."""
    assert wants_whole_file(recording_seconds=1800, clip_seconds=34) is False
    assert wants_whole_file(recording_seconds=300, clip_seconds=34) is False


def test_the_boundary_is_the_slack() -> None:
    """Stated explicitly, because it decides which route every event takes."""
    assert wants_whole_file(30 + WHOLE_FILE_SLACK_SECONDS, 30) is True
    assert wants_whole_file(30 + WHOLE_FILE_SLACK_SECONDS + 1, 30) is False


def test_a_recording_of_unknown_length_is_cut() -> None:
    """Better to ask for exactly the clip than to upload something unmeasured."""
    assert wants_whole_file(recording_seconds=0, clip_seconds=34) is False
