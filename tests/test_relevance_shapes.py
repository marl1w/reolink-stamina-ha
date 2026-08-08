"""Spelling a learned profile, and adding several of them together.

Two jobs, and the second is the one worth testing hard: merging is arithmetic on weighted
counts, and arithmetic that is quietly wrong produces a chart that looks entirely plausible.
"""

from __future__ import annotations

from custom_components.reolink_stamina.relevance.rates import Model, Profile
from custom_components.reolink_stamina.relevance.shapes import (
    categorical_shape,
    clock_shape,
    duration_phrase,
    predecessor_phrase,
    profile_payload,
    state_phrase,
)

_NAMES = {"entry1:0": "Drive", "entry1:1": "Gate"}


def _profile(camera_minutes: list[int], *, durations: tuple[str, ...] = ()) -> Profile:
    """Return a profile with events at the given minutes of the day."""
    profile = Profile()
    for minute in camera_minutes:
        profile.clock.add(minute, 1.0)
        profile.events += 1
    for label in durations:
        profile.duration.add(label, 1.0)
    return profile


def test_a_duration_bucket_reads_as_a_phrase():
    """A bucket like ~32s is a key, and nobody should be shown a key."""
    assert duration_phrase("~32s") == "About 32s"
    assert duration_phrase("~2s") == "About 2s"
    assert duration_phrase("~128s") == "About 2 min"
    assert duration_phrase("open") == "Still running"
    assert duration_phrase("instant") == "Instant"
    # Anything unrecognised is passed through rather than mangled into something wrong.
    assert duration_phrase("weird") == "weird"


def test_a_predecessor_reads_as_a_camera_and_a_gap():
    """The label is "camera|<30s", which is exactly what was on screen before this existed."""
    assert predecessor_phrase("entry1:0|<30s", _NAMES) == "Drive, within 30s"
    assert predecessor_phrase("entry1:0|<120s", _NAMES) == "Drive, within 2 min"
    # "Nothing" is a category, not an absence — on a quiet camera it is usually the commonest.
    assert predecessor_phrase("none", _NAMES) == "Nothing fired first"
    # A camera that has since been removed still has history, and still has to render.
    assert predecessor_phrase("entry9:7|<10s", _NAMES) == "entry9:7, within 10s"


def test_a_raw_state_is_spelled_not_interpreted():
    """The journal stores states verbatim; this only capitalises them."""
    assert state_phrase("armed_away") == "Armed away"
    assert state_phrase("on") == "On"
    assert state_phrase("") == ""


def test_the_clock_folds_to_hours_without_losing_events():
    """288 bins exist for the kernel, not for a reader. The fold must conserve the count."""
    profile = _profile([0, 30, 61, 61, 1439])
    hours = clock_shape(profile.clock)

    assert len(hours) == 24
    assert [hour["hour"] for hour in hours] == list(range(24))
    assert sum(hour["events"] for hour in hours) == sum(profile.clock.counts)
    assert hours[0]["events"] == 2, "00:00 and 00:30 are the same hour"
    assert hours[1]["events"] == 2
    assert hours[23]["events"] == 1


def test_a_categorical_comes_back_commonest_first():
    """The order is the finding: the folding below shows the top of this list."""
    profile = Profile()
    for label, times in (("rare", 1), ("common", 12), ("middling", 4)):
        for _ in range(times):
            profile.duration.add(label, 1.0)

    shaped = categorical_shape(profile.duration)
    assert [entry["value"] for entry in shaped] == ["common", "middling", "rare"]
    assert shaped[0]["events"] == 12
    assert sum(entry["share"] for entry in shaped) == 1.0


def test_one_camera_is_reported_as_itself():
    """The common case, and the one that must not gain a "which camera" row."""
    model = Model()
    model.profiles[("entry1:0", "person")] = _profile([480, 481], durations=("~8s",))
    model.per_camera["entry1:0"] = _profile([480, 481])
    model.thresholds["entry1:0"] = 3.5

    payload = profile_payload(model, ["entry1:0"], names=_NAMES)

    assert payload["threshold"] == 3.5
    assert [entry["kind"] for entry in payload["kinds"]] == ["person"]
    assert "cameras" not in payload["kinds"][0]
    assert payload["kinds"][0]["duration"][0]["label"] == "About 8s"


def test_several_cameras_add_up_and_say_which_was_which():
    """Weighted counts add. The chart is worthless if they add up wrong."""
    model = Model()
    model.profiles[("entry1:0", "person")] = _profile([480] * 9, durations=("~8s",) * 9)
    model.profiles[("entry1:1", "person")] = _profile([1080], durations=("~64s",))
    model.per_camera["entry1:0"] = _profile([480] * 9)
    model.per_camera["entry1:1"] = _profile([1080])

    payload = profile_payload(model, ["entry1:0", "entry1:1"], names=_NAMES)
    person = payload["kinds"][0]

    assert person["events"] == 10, "nine plus one, not nine and not one"
    hours = {hour["hour"]: hour["events"] for hour in person["clock"]}
    assert hours[8] == 9 and hours[18] == 1, "both cameras' hours are present"
    assert [(entry["label"], entry["events"]) for entry in person["cameras"]] == [
        ("Drive", 9),
        ("Gate", 1),
    ]
    assert sum(entry["events"] for entry in person["duration"]) == 10
    # No single camera's threshold can stand for the group.
    assert payload["threshold"] is None


def test_merging_does_not_mutate_what_it_merged():
    """A merge must build a new profile, never accumulate into one it was handed.

    The model is rebuilt nightly and read many times in between, so writing back into it would
    corrupt every later read until the next rebuild — and only for people who happened to open
    the overview, which is the worst kind of bug to be told about.
    """
    model = Model()
    first = _profile([480] * 3, durations=("~8s",) * 3)
    second = _profile([1080], durations=("~8s",))
    model.profiles[("entry1:0", "person")] = first
    model.profiles[("entry1:1", "person")] = second

    profile_payload(model, ["entry1:0", "entry1:1"], names=_NAMES)

    assert first.events == 3 and second.events == 1
    assert first.duration.counts == {"~8s": 3}
    assert sum(first.clock.counts) == 3


def test_a_camera_with_nothing_learned_is_empty_not_broken():
    """Every camera the panel lists gets asked, including ones that have never fired."""
    payload = profile_payload(Model(), ["entry9:4"])

    assert payload["kinds"] == []
    assert payload["all"] is None
