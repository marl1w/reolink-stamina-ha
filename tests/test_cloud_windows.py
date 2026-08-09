"""Tests for deciding what counts as one clip.

These are the rules that decide whether a person walking through a garden becomes one upload
or five, and whether someone standing still gets uploaded at all.
"""

from __future__ import annotations

import datetime as dt

from custom_components.reolink_stamina.cloud.windows import WindowCollector

CAMERA = "main|7"
BASE = dt.datetime(2026, 8, 4, 19, 50, 0)


def collector(*, lead=10.0, tail=10.0, settle=20.0, maximum=600.0) -> WindowCollector:
    """Return a collector with the defaults the syncer uses."""
    return WindowCollector(lead=lead, tail=tail, settle=settle, maximum=maximum)


def at(seconds: float) -> dt.datetime:
    """Return a moment relative to the start of the scenario."""
    return BASE + dt.timedelta(seconds=seconds)


def test_a_clip_covers_the_padding_either_side() -> None:
    """Ten seconds before the first detection to ten after the last thing cleared."""
    windows = collector()
    windows.record_on(CAMERA, "person", "person", at(0))
    windows.record_off(CAMERA, "person", at(15))

    assert windows.collect(at(30)) == []  # the tail has not elapsed
    ready = windows.collect(at(35))

    assert len(ready) == 1
    clip = ready[0]
    assert clip.start == at(-10)
    assert clip.end == at(25)
    assert clip.kinds == ("person",)
    assert clip.truncated is False


def test_someone_standing_still_is_not_cut_short() -> None:
    """The bug this rule exists for.

    A sensor that turns on and simply stays on produces no further events, so a collector
    watching only for detections would consider the camera quiet after the settle period and
    close the clip while the person was still in frame.
    """
    windows = collector()
    windows.record_on(CAMERA, "person", "person", at(0))

    # Two minutes later, still on: nothing may be handed over.
    assert windows.collect(at(120)) == []
    assert windows.active(CAMERA) is True

    windows.record_off(CAMERA, "person", at(120))
    assert windows.collect(at(130)) == []  # tail still running
    ready = windows.collect(at(141))

    assert len(ready) == 1
    assert ready[0].start == at(-10)
    assert ready[0].end == at(130), "the clip must run to ten seconds after they left"


def test_stepping_out_of_frame_and_back_is_one_clip() -> None:
    """Exactly the case asked about: off, then on again while the tail is running.

    The window is not finished, the tail starts over, and one clip covers the whole visit.
    """
    windows = collector()
    windows.record_on(CAMERA, "person", "person", at(0))
    windows.record_off(CAMERA, "person", at(10))
    # Back in frame eight seconds later, inside the twenty-second settle.
    windows.record_on(CAMERA, "person", "person", at(18))
    windows.record_off(CAMERA, "person", at(30))

    assert windows.collect(at(45)) == [], "the tail restarted when they came back"
    ready = windows.collect(at(51))

    assert len(ready) == 1, "one visit is one clip"
    assert ready[0].start == at(-10)
    assert ready[0].end == at(40)


def test_several_sensors_hold_the_window_between_them() -> None:
    """A car sets off motion, vehicle and person; the clip ends when the last one clears."""
    windows = collector()
    windows.record_on(CAMERA, "motion", "motion", at(0))
    windows.record_on(CAMERA, "vehicle", "vehicle", at(1))
    windows.record_on(CAMERA, "person", "person", at(3))
    windows.record_off(CAMERA, "motion", at(20))
    windows.record_off(CAMERA, "person", at(25))

    # Vehicle is still on, so nothing is finished however long we wait.
    assert windows.collect(at(300)) == []

    windows.record_off(CAMERA, "vehicle", at(300))
    ready = windows.collect(at(325))

    assert len(ready) == 1
    assert ready[0].kinds == ("motion", "person", "vehicle")
    assert ready[0].end == at(310)


def test_a_stuck_sensor_cannot_suppress_uploads_for_ever() -> None:
    """Reolink sensors do occasionally stay on; the clip is cut at the ceiling and flagged."""
    windows = collector(maximum=120.0)
    windows.record_on(CAMERA, "motion", "motion", at(0))

    assert windows.collect(at(119)) == []
    ready = windows.collect(at(121))

    assert len(ready) == 1
    assert ready[0].truncated is True, "the caller should know it is the start of something"
    assert ready[0].end == at(131), "ends now plus the tail, not at some sensor's whim"


def test_cameras_are_independent() -> None:
    """One person triggers several cameras; each gets its own clip."""
    windows = collector()
    windows.record_on("main|7", "person", "person", at(0))
    windows.record_on("main|3", "person", "person", at(2))
    windows.record_off("main|7", "person", at(5))
    windows.record_off("main|3", "person", at(40))

    first = windows.collect(at(26))
    assert [clip.key for clip in first] == ["main|7"]
    assert windows.pending == 1

    second = windows.collect(at(61))
    assert [clip.key for clip in second] == ["main|3"]
    assert windows.pending == 0


def test_nothing_is_taken_on_while_not_accepting() -> None:
    """With the switch off a detection opens no window, so nothing is gathered at all."""
    windows = collector()

    assert windows.record_on(CAMERA, "person", "person", at(0), accepting=False) is False
    assert windows.pending == 0

    windows.record_off(CAMERA, "person", at(5))
    assert windows.collect(at(60)) == []


def test_an_event_admitted_while_accepting_survives_the_switch_going_off() -> None:
    """The arrival bug, at the level the rule lives.

    A person at 21:51:01, the alarm disarmed at 21:51:07, the guard automation turned
    the switch off at 21:51:22, and the window did not settle until 21:51:46. Asking the switch
    at that point threw away the footage of the arrival — the one clip that mattered.
    """
    windows = collector()

    assert windows.record_on(CAMERA, "person", "person", at(0)) is True
    # The switch goes off here, twenty-one seconds in, while the event is still running.
    windows.record_off(CAMERA, "person", at(25))

    ready = windows.collect(at(46))

    assert len(ready) == 1, "an admitted event is still a clip"
    assert ready[0].start == at(-10)
    assert ready[0].end == at(35)


def test_a_returning_detection_extends_an_admitted_window_even_when_not_accepting() -> None:
    """Stepping out of frame and back is one visit, and the switch cannot split it in two.

    The second detection arrives after the switch went off, so it must be folded into the
    window it belongs to rather than refused as a new event.
    """
    windows = collector()
    windows.record_on(CAMERA, "person", "person", at(0))
    windows.record_off(CAMERA, "person", at(10))

    assert windows.record_on(CAMERA, "person", "person", at(18), accepting=False) is True
    windows.record_off(CAMERA, "person", at(30))

    assert windows.collect(at(45)) == [], "the tail restarted when they came back"
    ready = windows.collect(at(51))

    assert len(ready) == 1
    assert ready[0].end == at(40), "the clip covers the whole visit"


def test_the_switch_coming_back_admits_the_next_event() -> None:
    """Refusing an event must not latch: the next detection is judged on its own."""
    windows = collector()

    assert windows.record_on(CAMERA, "person", "person", at(0), accepting=False) is False
    assert windows.record_on(CAMERA, "person", "person", at(60), accepting=True) is True

    windows.record_off(CAMERA, "person", at(70))
    ready = windows.collect(at(91))

    assert len(ready) == 1
    assert ready[0].start == at(50), "the clip starts at the detection that was accepted"


def test_shutdown_keeps_what_it_has() -> None:
    """A restart mid-event should still produce the footage of what just happened."""
    windows = collector()
    windows.record_on(CAMERA, "person", "person", at(0))
    ready = windows.flush(at(30))

    assert len(ready) == 1
    assert ready[0].truncated is True
    assert ready[0].end == at(40)
    assert windows.pending == 0


def test_nothing_open_is_never_due() -> None:
    """A syncer with no event gathering has nothing to set a timer for."""
    assert collector().next_due() is None


def test_a_quiet_window_is_due_when_its_tail_elapses() -> None:
    """What lets the syncer look at exactly the right moment rather than sweeping.

    The sweep runs every five seconds, so every clip was paying an average of two and a half
    seconds before it was even queued.
    """
    windows = collector(settle=15.0)
    windows.record_on(CAMERA, "person", "person", at(0))
    windows.record_off(CAMERA, "person", at(4))

    assert windows.next_due() == at(19), "fifteen seconds after the camera went quiet"


def test_a_window_with_something_still_in_frame_is_due_at_the_ceiling() -> None:
    """Nothing can close it before then, so looking earlier would find nothing."""
    windows = collector(settle=15.0, maximum=600.0)
    windows.record_on(CAMERA, "person", "person", at(0))

    assert windows.next_due() == at(600)


def test_the_earliest_of_several_cameras_is_what_is_returned() -> None:
    """One timer serves every open window, so it has to be set for the first of them."""
    windows = collector(settle=15.0)
    windows.record_on("main|1", "person", "person", at(0))
    windows.record_off("main|1", "person", at(30))
    windows.record_on("main|2", "person", "person", at(0))
    windows.record_off("main|2", "person", at(5))

    assert windows.next_due() == at(20), "the camera that went quiet first"
