"""What the model learns, and whether it says the right thing about it.

The scenario these are built around is the one the whole feature was argued from: a cat
crosses the drive at one in the morning every night, and people come home at six in the
evening. A person at one in the morning is the event worth marking — and nothing anywhere
has to know what a cat is for that to fall out.
"""

from __future__ import annotations

from dataclasses import replace
import datetime as dt
import math

import pytest

from custom_components.reolink_stamina.const import (
    RELEVANCE_SCOPE_CAMERA,
    RELEVANCE_SCOPE_RECORDER,
    RELEVANCE_SCOPE_TOGETHER,
)
from custom_components.reolink_stamina.relevance.events import Event
from custom_components.reolink_stamina.relevance.rates import (
    CircularRate,
    band_label,
    bands,
    build,
    duration_bucket,
    gap,
    group_key,
    interpolate,
    lag_bucket,
    preceded,
    predecessor_label,
    quantile,
    recency,
    signal_value,
)
from custom_components.reolink_stamina.relevance.score import (
    Score,
    Term,
    calibrate,
    ready,
    score,
)

_CAMERA = "entry1:0"
# A camera on a second recorder, at a second property. See `_two_properties`.
_OTHER_NVR = "entry2:0"
_NOW = 1_800_000_000.0
_DAY = 86400.0


def _event(started_at: float, minute: int, kind: str, *, duration: float = 8.0) -> Event:
    """Build one event, with the solar term deliberately absent.

    Sunset would be a second reading of the same clock, and these tests are about whether the
    clock term works rather than about counting it twice.
    """
    # The day comes from the timestamp, exactly as `derive` works it out. Hardcoding it — or
    # leaving it at its default — gave every event in a 180-day history the same phantom day,
    # which the day-of-week term then read as the single most predictable household on earth.
    local = dt.datetime.fromtimestamp(started_at)
    return Event(
        camera=_CAMERA,
        kind=kind,
        started_at=started_at,
        ended_at=started_at + duration,
        duration=duration,
        minute_of_day=minute,
        solar_offset=None,
        solar_phase=None,
        is_weekend=local.weekday() >= 5,
        day_of_week=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()],
    )


def _history(days: int = 180) -> list[Event]:
    """Return a boring household: the cat at 01:00, people home at 18:00."""
    events: list[Event] = []
    for day in range(days):
        base = _NOW - (days - day) * _DAY
        events.append(_event(base + 3600, 60, "animal"))
        events.append(_event(base + 18 * 3600, 18 * 60, "person"))
        events.append(_event(base + 18 * 3600 + 600, 18 * 60 + 10, "person"))
    events.sort(key=lambda item: item.started_at)
    return events


def _model(*, floor: float = 0.0):
    """Return a model trained on the boring household, already calibrated.

    Calibrated with no floor by default, and that is a statement about what these tests are
    for. Most of them ask whether the *ranking* is right and whether a camera is compared with
    itself — the floor is a separate claim, tested on its own below, and letting it into every
    fixture would mean a change to one number quietly rewriting what forty tests assert.

    The synthetic household here is also far smaller than a real one, so its surprisals are
    compressed: a person at 01:00 scores 0.65 nats against a few hundred invented events where
    the same event on a real installation clears 2. Tuning the shipped floor to make this
    fixture pass would be fitting a constant to a fixture.
    """
    events = _history()
    model = build(events, now=_NOW)
    calibrate(model, events, floor=floor)
    return model, events


# ------------------------------------------------------------------ primitives


def test_recency_halves_over_the_half_life():
    """Ninety days is the point at which the past counts half as much."""
    assert recency(0.0) == 1.0
    assert math.isclose(recency(90.0), 0.5)
    assert math.isclose(recency(180.0), 0.25)


def test_the_clock_wraps_at_midnight():
    """23:59 and 00:01 are two minutes apart, not twenty-three hours."""
    rate = CircularRate()
    rate.add(0, 1.0)

    assert rate.probability(1439) > rate.probability(720)


def test_a_bin_with_nothing_near_it_is_rare_but_not_impossible():
    """One term at infinity would drown out every other."""
    rate = CircularRate()
    for _ in range(500):
        rate.add(18 * 60, 1.0)

    assert 0.0 < rate.probability(3 * 60) < rate.probability(18 * 60)


def test_durations_are_bucketed_coarsely():
    """Rarity is counted, and a continuous value never repeats, so it could never be rare."""
    assert duration_bucket(9.0) == duration_bucket(15.0)
    assert duration_bucket(9.0) != duration_bucket(200.0)
    assert duration_bucket(None) == "open"


def test_a_distant_predecessor_is_the_same_as_none():
    """Beyond a couple of minutes, one camera firing after another is a coincidence."""
    assert lag_bucket(None) == "none"
    assert lag_bucket(5000.0) == "none"
    assert lag_bucket(5.0) != "none"


def _at(camera: str, started_at: float, duration: float) -> Event:
    """One event on a named camera, of a stated length. For the window tests below."""
    return replace(_event(started_at, 600, "vehicle", duration=duration), camera=camera)


def test_the_window_measures_the_quiet_in_between_rather_than_start_to_start():
    """A long detection can still be what came first.

    Measured start to start, a detection lasting longer than the window could never be a
    predecessor however tightly the next one followed: a car sitting in the drive for three
    minutes and a person at the door two seconds after it cleared came out as "nothing fired
    first", which is precisely backwards.
    """
    car = _at("entry1:0", _NOW, 180.0)
    person = _at("entry1:1", _NOW + 182.0, 5.0)

    assert gap(person, car) == 2.0
    assert predecessor_label(person, car) == "entry1:0|<10s"


def test_a_quiet_gap_past_the_window_is_still_nothing():
    """The window is the point. Twenty minutes of quiet is not somebody walking."""
    gate = _at("entry1:0", _NOW, 8.0)
    later = _at("entry1:1", _NOW + 20 * 60, 8.0)

    assert predecessor_label(later, gate) == "none"


def test_two_cameras_seeing_the_same_person_at_once_is_the_strongest_after():
    """Overlap floors at zero rather than going negative."""
    drive = _at("entry1:0", _NOW, 30.0)
    hall = _at("entry1:1", _NOW + 10.0, 8.0)

    assert gap(hall, drive) == 0.0
    assert predecessor_label(hall, drive) == "entry1:0|<10s"


def test_an_event_still_running_is_measured_from_where_it_began():
    """No end recorded, so there is nothing else to measure from."""
    open_run = replace(_at("entry1:0", _NOW, 8.0), ended_at=None, duration=None)
    after = _at("entry1:1", _NOW + 25.0, 8.0)

    assert gap(after, open_run) == 25.0
    assert predecessor_label(after, open_run) == "entry1:0|<30s"


def test_interpolation_leans_on_the_fallback_when_evidence_is_thin():
    """It is what stops a camera added last Tuesday finding everything remarkable."""
    assert interpolate(1.0, 0.0, 0.0) == 0.0
    assert interpolate(1.0, 0.0, 10_000.0) > 0.99


def test_quantile_is_nearest_rank():
    """The sample is scores and the answer is a cut; inventing a value between two buys nothing."""
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert quantile([], 0.5) == math.inf


# ---------------------------------------------------------------- the scenario


def test_a_person_at_the_usual_hour_is_unremarkable():
    """Most of what a camera sees has to score low, or the mark means nothing."""
    model, _ = _model()

    result = score(_event(_NOW, 18 * 60, "person"), model, previous=None)

    assert result.unusual is False


def test_a_person_in_the_middle_of_the_night_stands_out():
    """The event the whole feature exists for."""
    model, _ = _model()

    usual = score(_event(_NOW, 18 * 60, "person"), model, previous=None)
    odd = score(_event(_NOW, 60, "person"), model, previous=None)

    assert odd.total > usual.total
    assert odd.unusual is True


def test_the_cat_at_the_same_hour_is_not_unusual():
    """Rarity is per kind: the same minute means different things for different subjects."""
    model, _ = _model()

    person = score(_event(_NOW, 60, "person"), model, previous=None)
    animal = score(_event(_NOW, 60, "animal"), model, previous=None)

    assert animal.total < person.total
    assert animal.unusual is False


def test_an_unusually_long_visit_scores_on_its_duration():
    """Not every odd event is at an odd hour; a loiterer arrives at teatime."""
    model, _ = _model()

    brief = score(_event(_NOW, 18 * 60, "person", duration=8.0), model, previous=None)
    lingering = score(_event(_NOW, 18 * 60, "person", duration=600.0), model, previous=None)

    assert lingering.total > brief.total
    assert _term(lingering, "duration") > _term(brief, "duration")


def _term(result: Score, name: str) -> float:
    """Return one term's contribution."""
    return next(term.contribution for term in result.terms if term.name == name)


def test_contributions_are_signed_against_chance():
    """Positive is rarer than chance, negative is more common.

    Which is what makes a breakdown legible to somebody who has never heard of a nat.
    """
    model, _ = _model()

    expected = _term(score(_event(_NOW, 18 * 60, "person"), model, previous=None), "clock")
    surprising = _term(score(_event(_NOW, 60, "person"), model, previous=None), "clock")

    assert expected < 0 < surprising


def test_the_predecessor_must_be_stated_rather_than_assumed():
    """The predecessor is required rather than defaulted.

    The threshold is calibrated over one set of terms, so scoring a different set would
    compare two numbers measured on different scales and mark nothing at all.
    """
    model, _ = _model()

    with pytest.raises(TypeError):
        score(_event(_NOW, 18 * 60, "person"), model)  # type: ignore[call-arg]


def test_training_and_scoring_agree_on_what_a_predecessor_is():
    """They are two readers of one table, and a disagreement is invisible until nothing works.

    Training once recorded `camera|none` for a gap too long to matter while scoring looked up
    a bare `none`, so the commonest situation in a quiet household was never found and every
    ordinary event read as though it had never happened before.
    """
    model, events = _model()
    profile = model.profile(_CAMERA, "person")

    label = predecessor_label(events[-1], events[-2])
    assert profile.predecessor.count(label) > 0


# ------------------------------------------------------------------ readiness


def test_a_camera_with_no_history_is_not_scored_at_all():
    """Still collecting is not the same as "nothing here is unusual"."""
    events = _history(days=3)
    model = build(events, now=_NOW)
    calibrate(model, events)

    result = score(_event(_NOW, 60, "person"), model, previous=None)

    assert result.threshold is None
    assert result.unusual is False


def test_readiness_needs_both_time_and_events():
    """A camera can have months of days and still too few detections to compare against."""
    busy_but_new = build(
        [_event(_NOW - hour * 3600, 60, "person") for hour in range(300)], now=_NOW
    )
    assert ready(busy_but_new.per_camera[_CAMERA]) is False

    model, _ = _model()
    assert ready(model.per_camera[_CAMERA]) is True


def test_the_odd_ones_out_are_what_gets_marked():
    """The end-to-end claim: a handful of genuinely strange events, and nothing else."""
    events = _history()
    intruders = [_event(_NOW - day * _DAY + 3 * 3600, 3 * 60, "person") for day in (5, 40, 90)]
    events = sorted([*events, *intruders], key=lambda item: item.started_at)

    model = build(events, now=_NOW)
    calibrate(model, events, floor=0.0)

    previous = None
    marked = []
    for event in events:
        if score(event, model, previous=previous).unusual:
            marked.append(event)
        previous = event

    assert len(marked) <= len(events) * 0.05
    assert all(event in marked for event in intruders)


def test_a_negative_threshold_cannot_mark_anything_on_its_own():
    """The floor, and the bug it exists for.

    A quantile always cuts somewhere, however ordinary the history behind it — so a camera
    whose life is entirely predictable still marks its top few percent. Measured on a real
    installation, the per-camera thresholds at the 0.95 quantile ran from -0.63 to 0.45: a
    negative threshold marks events that were *more* likely than chance, which is how a person
    seen for the seventh time in ten days came to be called unusual.
    """
    events = _history()
    model = build(events, now=_NOW)
    calibrate(model, events, share=0.95, floor=1.4)

    assert model.thresholds[_CAMERA] < 1.4, "this fixture is the case worth guarding"

    # Comfortably the strangest thing this household has ever produced, and still short of the
    # floor on a synthetic history this small.
    odd = score(_event(_NOW, 60, "person"), model, previous=None)
    assert odd.total > model.thresholds[_CAMERA], "it clears the camera's own line"
    assert odd.unusual is False, "and is not marked, because it does not clear the floor"
    assert odd.threshold == 1.4, "the panel is shown the line actually measured against"


def test_the_floor_is_what_the_sensitivity_setting_moves():
    """Three words, three floors — and the mark count has to follow them."""
    events = _history()
    intruders = [_event(_NOW - day * _DAY + 3 * 3600, 3 * 60, "person") for day in (5, 40, 90)]
    events = sorted([*events, *intruders], key=lambda item: item.started_at)

    counts = []
    for floor in (0.0, 1.0, 3.0):
        model = build(events, now=_NOW)
        calibrate(model, events, floor=floor)
        previous = None
        marked = 0
        for event in events:
            if score(event, model, previous=previous).unusual:
                marked += 1
            previous = event
        counts.append(marked)

    # Non-increasing rather than strictly decreasing: this household produces exactly three
    # strange events, so the first two floors both find the same three. What must never happen
    # is a higher floor finding *more*.
    assert counts == sorted(counts, reverse=True), f"a higher floor cannot mark more: {counts}"
    assert counts[0] > 0 and counts[-1] == 0, "and a high enough floor marks nothing at all"


def test_a_weekend_is_not_unusual_merely_for_being_a_weekend():
    """The trap this term walks into if its baseline is wrong.

    Measured against an even split, "it was a Saturday" is rare in every household there has
    ever been — because a Saturday is two days in seven — and every weekend event would carry
    a large positive contribution for a fact about the calendar. The baseline is the calendar's
    own proportion, so a household that behaves identically all week contributes nothing.
    """
    model, _ = _model()

    # This history is the same three events every single day, so no day is special.
    weekday = _term(score(_event(_NOW, 18 * 60, "person"), model, previous=None), "weekend")
    saturday = _NOW + (5 - dt.datetime.fromtimestamp(_NOW).weekday()) % 7 * _DAY
    weekend = _term(score(_event(saturday, 18 * 60, "person"), model, previous=None), "weekend")

    assert abs(weekday) < 0.35, f"an ordinary weekday should say almost nothing, said {weekday}"
    assert abs(weekend) < 0.35, f"nor should an ordinary weekend, said {weekend}"


def test_a_camera_with_little_history_is_judged_on_weekday_against_weekend():
    """Seven categories need seven times the history; two are affordable immediately.

    Both are counted and the scorer backs off between them, so there is no threshold at which
    one becomes the other — a camera simply earns the finer judgement as it collects.
    """
    quiet = [_event(_NOW - day * _DAY, 18 * 60, "person") for day in range(21)]
    model = build(quiet, now=_NOW)

    profile = model.profiles[(_CAMERA, "person")]
    assert profile.day_of_week.total / 7.0 < 40.0, "this camera has not earned seven categories"

    # Every day is represented three times over, so nothing is remarkable whichever table wins.
    for day in range(7):
        contribution = _term(
            score(_event(_NOW - day * _DAY, 18 * 60, "person"), model, previous=None), "weekend"
        )
        assert abs(contribution) < 0.6, f"day {day} scored {contribution} on an even history"


def test_a_busy_camera_can_tell_one_day_from_another():
    """A gate that never sees anyone on a Sunday should say so when it does."""
    events = []
    for offset in range(210):
        at = _NOW - offset * _DAY
        # The actual weekday of that timestamp, not the loop counter: an offset of six days
        # from today is some weekday or other, and skipping it skips whichever one that is.
        if dt.datetime.fromtimestamp(at).weekday() == 6:
            continue  # nothing ever happens on a Sunday
        for _ in range(4):
            events.append(_event(at, 18 * 60, "person"))
    events.sort(key=lambda item: item.started_at)
    model = build(events, now=_NOW)

    sunday = _NOW - ((dt.datetime.fromtimestamp(_NOW).weekday() - 6) % 7) * _DAY
    assert dt.datetime.fromtimestamp(sunday).weekday() == 6

    unheard_of = _term(score(_event(sunday, 18 * 60, "person"), model, previous=None), "weekend")
    ordinary = _term(score(events[-1], model, previous=None), "weekend")

    assert unheard_of > 1.0, f"a Sunday it has never seen should stand out, scored {unheard_of}"
    assert unheard_of > ordinary


def test_a_signal_that_never_reports_is_not_counted_at_all():
    """A siren that has never fired is not evidence, and it was being treated as strong.

    One category means a probability of one, and a surprisal measured against two categories
    is then a *constant* negative — subtracted from every score on that camera for ever. It
    cost nothing to notice and it moved the line for everything else.
    """
    silent = _with(_history(), "siren.never_fired", ["unknown"])
    model = build(silent, now=_NOW)
    calibrate(model, silent, floor=0.0)

    result = score(silent[-1], model, previous=None)
    assert not [term for term in result.terms if term.name == "signal"], (
        "a signal with nothing to say should contribute no term"
    )

    # And the score is the same as it would be with the signal absent entirely.
    plain = _history()
    bare = build(plain, now=_NOW)
    calibrate(bare, plain, floor=0.0)
    assert math.isclose(result.total, score(plain[-1], bare, previous=None).total, abs_tol=1e-9)


def test_a_signal_that_is_usually_absent_still_counts_when_it_reports():
    """The guard is about signals with *nothing* to say, not about ones that are often quiet."""
    mostly = _with(_history(), "binary_sensor.rare", ["unknown"] * 9 + ["on"])
    model = build(mostly, now=_NOW)
    calibrate(model, mostly, floor=0.0)

    result = score(mostly[-1], model, previous=None)
    assert [term for term in result.terms if term.name == "signal"], (
        "one real reading is enough to make a signal worth counting"
    )


# --------------------------------------------------------------- numeric signals


def _with(events, entity: str, values):
    """Attach a numeric signal reading to each event, cycling through the values given."""
    return [
        Event(
            camera=item.camera,
            kind=item.kind,
            started_at=item.started_at,
            ended_at=item.ended_at,
            duration=item.duration,
            minute_of_day=item.minute_of_day,
            solar_offset=item.solar_offset,
            solar_phase=item.solar_phase,
            is_weekend=item.is_weekend,
            # Carried, not defaulted. Leaving it out gave every rebuilt event the same phantom
            # day, which the day term then read as a household of remarkable regularity — and
            # made two otherwise identical models score differently.
            day_of_week=item.day_of_week,
            context=((entity, str(values[index % len(values)])),),
        )
        for index, item in enumerate(events)
    ]


def test_a_continuous_reading_is_cut_into_bands_from_its_own_history():
    """A number never repeats, so it could never be rare. Bands are what make it countable.

    Learned rather than fixed: any set of edges chosen in advance is wrong for every
    installation but one, and "brighter than four days in five" has to mean the same thing
    under a porch light and facing a field.
    """
    assert bands([float(n) for n in range(100)]) == (19.0, 39.0, 59.0, 79.0)
    assert band_label(5.0, (19.0, 39.0, 59.0, 79.0)) == "< 19"
    assert band_label(45.0, (19.0, 39.0, 59.0, 79.0)) == "39 to 59"
    assert band_label(95.0, (19.0, 39.0, 59.0, 79.0)) == "79 and up"


def test_a_sensor_that_barely_moves_is_not_cut_at_all():
    """A thermostat sitting at 21 all winter would make five bands holding one value."""
    assert bands([21.0] * 200) == ()
    # And too little history to cut is left alone rather than cut badly.
    assert bands([float(n) for n in range(10)]) == ()


def test_training_and_scoring_agree_on_which_band_a_reading_is():
    """They once did not, for the predecessor label, and every ordinary event scored wrong."""
    events = _with(_history(), "sensor.lux", list(range(0, 200, 7)))
    model = build(events, now=_NOW)
    calibrate(model, events, floor=0.0)

    assert model.signal_bands["sensor.lux"], "the readings were varied enough to band"

    # The value the scorer looks up has to be the one the training pass wrote down.
    learned = model.profiles[(_CAMERA, "person")].signals["sensor.lux"]
    result = score(
        Event(
            camera=_CAMERA,
            kind="person",
            started_at=_NOW,
            ended_at=_NOW + 8,
            duration=8.0,
            minute_of_day=18 * 60,
            solar_offset=None,
            solar_phase=None,
            is_weekend=False,
            context=(("sensor.lux", "45.0"),),
        ),
        model,
        previous=None,
    )
    term = next(item for item in result.terms if item.name == "signal")
    assert term.label in learned.weights, f"{term.label} was never counted during training"
    assert term.seen > 0, "and it has a count behind it, not a guess"


def test_a_reading_outside_everything_ever_seen_is_the_rare_one():
    """The point of the exercise: a night far darker than any before it has to stand out."""
    events = _with(_history(), "sensor.lux", list(range(100, 140)))
    model = build(events, now=_NOW)
    calibrate(model, events, floor=0.0)

    def _lux(reading: str) -> float:
        result = score(
            Event(
                camera=_CAMERA,
                kind="person",
                started_at=_NOW,
                ended_at=_NOW + 8,
                duration=8.0,
                minute_of_day=18 * 60,
                solar_offset=None,
                solar_phase=None,
                is_weekend=False,
                context=(("sensor.lux", reading),),
            ),
            model,
            previous=None,
        )
        return next(item.contribution for item in result.terms if item.name == "signal")

    assert _lux("0.0") > _lux("120.0"), "a reading below anything ever seen is the surprising one"


def test_a_state_that_is_not_a_number_is_left_as_itself():
    """`unavailable` is a category, and a sensor's bands must not swallow it."""
    events = _with(_history(), "sensor.lux", list(range(0, 200, 7)))
    model = build(events, now=_NOW)

    assert signal_value("sensor.lux", "unavailable", model) == "unavailable"
    assert signal_value("sensor.alarm", "armed_away", model) == "armed_away"


# -------------------------------------------------------------------- the words


def test_the_sentence_says_what_stood_out_and_how_often():
    """A mark that cannot say why is a machine asking to be trusted, and it has not earned it."""
    model, _ = _model()

    result = score(_event(_NOW, 60, "person"), model, previous=None, names={_CAMERA: "Drive"})

    assert "Person" in result.reason
    assert "Drive" in result.reason
    assert "time in" in result.reason
    assert result.reason.endswith(".")


def test_an_ordinary_event_says_so_plainly():
    """Seeing why a common event is common is what makes the mark believable when it appears."""
    model, _ = _model()

    result = score(_event(_NOW, 18 * 60, "person"), model, previous=None, names={_CAMERA: "Drive"})

    assert "usual" in result.reason


def test_the_sentence_survives_not_knowing_a_camera_name():
    """Names come from the registry, and the scorer must not depend on one being there."""
    model, _ = _model()

    assert "This camera" in score(_event(_NOW, 60, "person"), model, previous=None).reason


def test_nothing_firing_first_is_a_term_of_its_own():
    """Appearing without using the approach route is the most telling signal in the design."""
    model, _ = _model()

    result = score(_event(_NOW, 60, "person"), model, previous=None)

    assert any(term.name == "predecessor" for term in result.terms)
    assert result.total >= score(_event(_NOW, 60, "person"), model, previous=None).total - 1e-9


# ------------------------------------------------------------------- signals


def _with_signal(started_at: float, minute: int, kind: str, home: str) -> Event:
    """One event, carrying whether anybody was in."""
    return Event(
        camera=_CAMERA,
        kind=kind,
        started_at=started_at,
        ended_at=started_at + 8.0,
        duration=8.0,
        minute_of_day=minute,
        solar_offset=None,
        solar_phase=None,
        is_weekend=False,
        context=(("binary_sensor.someone_home", home),),
    )


def _signalled_history(days: int = 180) -> list[Event]:
    """Return a household that is in when the cameras see people, as households are."""
    events: list[Event] = []
    for day in range(days):
        base = _NOW - (days - day) * _DAY
        events.append(_with_signal(base + 18 * 3600, 18 * 60, "person", "on"))
        events.append(_with_signal(base + 18 * 3600 + 600, 18 * 60 + 10, "person", "on"))
        events.append(_with_signal(base + 3600, 60, "animal", "on"))
    events.sort(key=lambda item: item.started_at)
    return events


def test_a_signal_seen_for_the_first_time_stands_out():
    """Somebody on the drive while the house is empty is not the same event.

    Which is the entire point of letting a household point this at its own entities.
    """
    events = _signalled_history()
    model = build(events, now=_NOW)
    calibrate(model, events)

    usual = score(_with_signal(_NOW, 18 * 60, "person", "on"), model, previous=None)
    away = score(_with_signal(_NOW, 18 * 60, "person", "off"), model, previous=None)

    assert away.total > usual.total
    assert _term(away, "signal") > _term(usual, "signal")


def test_a_signal_names_itself_in_the_sentence():
    """A state on its own says nothing: "with armed_away" does not say what was armed."""
    events = _signalled_history()
    model = build(events, now=_NOW)
    calibrate(model, events)

    result = score(
        _with_signal(_NOW, 60, "person", "off"),
        model,
        previous=None,
        names={_CAMERA: "Drive"},
        labels={"binary_sensor.someone_home": "Someone home"},
    )

    assert any(term.subject == "Someone home" for term in result.terms)
    assert "Someone home" in result.reason


def test_history_from_before_a_signal_existed_is_not_a_hole():
    """History from before a signal existed is a value, not a hole.

    A signal added six months in was genuinely unknown until then, and dropping those events
    would shift every count that mentions it.
    """
    events = _signalled_history(days=60)
    # The older half predates the signal entirely. `Event` is slotted, so it is rebuilt
    # rather than copied through a `__dict__` it does not have.
    events = [
        item
        if item.started_at > _NOW - 30 * _DAY
        else Event(
            camera=item.camera,
            kind=item.kind,
            started_at=item.started_at,
            ended_at=item.ended_at,
            duration=item.duration,
            minute_of_day=item.minute_of_day,
            solar_offset=item.solar_offset,
            solar_phase=item.solar_phase,
            is_weekend=item.is_weekend,
            context=(),
        )
        for item in events
    ]
    model = build(events, now=_NOW)
    calibrate(model, events)

    result = score(_with_signal(_NOW, 18 * 60, "person", "on"), model, previous=None)

    assert any(term.name == "signal" for term in result.terms)
    assert math.isfinite(result.total)


# ---------------------------------------------------------------------- scope


def _two_properties(days: int = 180) -> list[Event]:
    """Return two recorders whose households have nothing in common.

    The first is the boring household above. The second is nocturnal: its people come and go
    at two in the morning, every night, and have done for six months. Neither has anything to
    say about the other, which is exactly the claim the scope setting exists to let somebody
    make.
    """
    events = list(_history(days))
    for day in range(days):
        base = _NOW - (days - day) * _DAY
        for started_at, minute in ((base + 2 * 3600, 120), (base + 2 * 3600 + 600, 130)):
            events.append(replace(_event(started_at, minute, "person"), camera=_OTHER_NVR))
    events.sort(key=lambda item: item.started_at)
    return events


def test_the_pool_a_camera_backs_off_to_follows_the_scope():
    """The whole point: a thin profile borrows from its own pool and no other.

    Two properties, one nocturnal. Pooled together, the first property's cameras have seen
    2 a.m. arrivals several hundred times — through the other recorder — and a person at 2
    a.m. there is that much less surprising. Kept apart, it is unheard of.
    """
    events = _two_properties()
    together = build(events, now=_NOW, scope=RELEVANCE_SCOPE_TOGETHER)
    apart = build(events, now=_NOW, scope=RELEVANCE_SCOPE_RECORDER)

    intruder = _event(_NOW, 120, "person")
    assert (
        score(intruder, apart, previous=None).total > score(intruder, together, previous=None).total
    )


def test_a_camera_kept_to_itself_has_nothing_to_borrow():
    """Under the per-camera scope every fallback collapses onto the camera's own profile."""
    events = _two_properties()
    model = build(events, now=_NOW, scope=RELEVANCE_SCOPE_CAMERA)

    specific, broader, pooled = model.blended(_CAMERA, "person")

    assert broader.events == specific.events
    assert pooled.events == model.per_camera[_CAMERA].events


def test_nothing_at_another_property_counts_as_having_fired_first():
    """A camera two hundred miles away did not precede anything.

    Recorded as much as scored: the two must agree on the label or the commonest situation in
    a quiet household is never found in the table.
    """
    events = _two_properties()
    apart = build(events, now=_NOW, scope=RELEVANCE_SCOPE_RECORDER)

    labels = {
        predecessor_label(event, previous)
        for event, previous in preceded(events, scope=RELEVANCE_SCOPE_RECORDER)
        if event.camera == _CAMERA
    }

    assert labels, "this fixture is meant to produce predecessors"
    assert not any(_OTHER_NVR in label for label in labels)
    assert all(
        _OTHER_NVR not in label for label in apart.profiles[(_CAMERA, "person")].predecessor.weights
    )


def test_a_group_is_read_off_the_camera_key_rather_than_looked_up():
    """A recorder that has since been unplugged still groups its history correctly."""
    assert group_key("entry1:3", RELEVANCE_SCOPE_RECORDER) == "entry1"
    assert group_key("entry1:3", RELEVANCE_SCOPE_CAMERA) == "entry1:3"
    assert group_key("entry1:3", RELEVANCE_SCOPE_TOGETHER) == group_key(
        "entry2:0", RELEVANCE_SCOPE_TOGETHER
    )


def test_each_camera_still_keeps_its_own_profile_whatever_the_scope():
    """Scope changes what may be borrowed, never what is counted."""
    events = _two_properties()
    counts = {
        scope: {
            camera: profile.events
            for camera, profile in build(events, now=_NOW, scope=scope).per_camera.items()
        }
        for scope in (
            RELEVANCE_SCOPE_TOGETHER,
            RELEVANCE_SCOPE_RECORDER,
            RELEVANCE_SCOPE_CAMERA,
        )
    }

    assert len(set(map(str, counts.values()))) == 1


def test_the_predecessor_says_how_long_ago_so_it_can_be_checked():
    """Naming the camera alone sends the reader to a row that is not the one meant.

    The detection that preceded an event is often motion, and the timeline hides motion by
    default — so the nearest visible row for the named camera can be forty minutes back, and
    the sentence looks wrong when it is right. The gap is what makes it checkable.
    """
    model, _ = _model()
    gate = _at("entry1:0", _NOW - 45.0, 8.0)
    hall = _at("entry1:1", _NOW, 8.0)

    term = next(
        t
        for t in score(hall, model, previous=gate, names={"entry1:0": "Gate"}).terms
        if t.name == "predecessor"
    )

    assert term.label == "37s after Gate"


def test_cameras_seeing_the_same_person_at_once_read_as_alongside():
    """Most of what a house full of cameras records. "0s after" reads as a rounding error."""
    model, _ = _model()
    drive = _at("entry1:0", _NOW, 30.0)
    hall = _at("entry1:1", _NOW + 10.0, 8.0)

    term = next(
        t
        for t in score(hall, model, previous=drive, names={"entry1:0": "Drive"}).terms
        if t.name == "predecessor"
    )

    assert term.label == "alongside Drive"


def test_nothing_first_still_says_so_plainly():
    """The absent case is the interesting one and must not grow a number."""
    model, _ = _model()

    term = next(
        t
        for t in score(_at("entry1:1", _NOW, 8.0), model, previous=None).terms
        if t.name == "predecessor"
    )

    assert term.label == "nothing fired first"


# ----------------------------------------------------------------- what it was


def _on(camera: str, started_at: float, minute: int, kind: str, duration: float) -> Event:
    """One event on a named camera, at a stated minute of the day."""
    return replace(_event(started_at, minute, kind, duration=duration), camera=camera)


_BALCONY = "entry1:5"


def _mostly_motion(days: int = 180) -> list[Event]:
    """Return a balcony camera: motion four times a day, a person every tenth day.

    The shape the kind term exists for. Every other term is measured against a profile the
    kind was used to select, so none of them can say that a person here is rare at all.
    """
    events: list[Event] = []
    for day in range(days):
        base = _NOW - (days - day) * _DAY
        for hour in (7, 12, 19, 22):
            events.append(_on(_BALCONY, base + hour * 3600, hour * 60, "motion", 20.0))
        if day % 10 == 0:
            events.append(_on(_BALCONY, base + 19 * 3600, 19 * 60, "person", 8.0))
    events.sort(key=lambda item: item.started_at)
    return events


def test_a_kind_a_camera_rarely_sees_is_scored_as_such():
    """The whole point: a person on a motion-only camera is unusual before anything else."""
    model = build(_mostly_motion(), now=_NOW)

    # Seven in the evening is this camera's busiest hour for both, so the clock cannot be
    # what separates them. Only the kind can.
    def kind_term(kind: str, duration: float) -> Term:
        result = score(_on(_BALCONY, _NOW, 19 * 60, kind, duration), model, previous=None)
        return next(term for term in result.terms if term.name == "kind")

    person, motion = kind_term("person", 8.0), kind_term("motion", 20.0)

    assert person.contribution > 0 > motion.contribution
    assert person.label == "Person"
    assert person.seen == 18


def test_the_kind_term_is_skipped_where_a_camera_has_seen_only_one():
    """Its probability is 1, so it would subtract a constant from every score for ever.

    The same trap the configured signals guard against: a term that cannot vary is not a
    term, it is an offset, and it moves the line for that camera and nothing else.
    """
    events = [_event(_NOW - hour * 3600, 60, "person") for hour in range(300)]
    model = build(events, now=_NOW)

    terms = score(_event(_NOW, 60, "person"), model, previous=None).terms

    assert not any(term.name == "kind" for term in terms)


def test_the_kind_reads_as_a_clause_in_both_positions():
    """A phrase appended after "and" has to stand up alone; "at all" does not."""
    events = _mostly_motion()
    model = build(events, now=_NOW)
    calibrate(model, events, floor=0.0)
    names = {_BALCONY: "Balcone Nord"}

    # Leading: at this camera's busiest hour, being a person at all is the whole story.
    leading = score(_on(_BALCONY, _NOW, 19 * 60, "person", 8.0), model, previous=None, names=names)
    assert "Person at all on Balcone Nord" in leading.reason

    # Trailing: three in the morning is stranger still, so the kind falls to second place
    # and has to become a clause of its own.
    trailing = score(_on(_BALCONY, _NOW, 3 * 60, "person", 8.0), model, previous=None, names=names)
    assert trailing.reason.endswith("and rarely seen here.")
