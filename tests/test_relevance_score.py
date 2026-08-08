"""What the model learns, and whether it says the right thing about it.

The scenario these are built around is the one the whole feature was argued from: a cat
crosses the drive at one in the morning every night, and people come home at six in the
evening. A person at one in the morning is the event worth marking — and nothing anywhere
has to know what a cat is for that to fall out.
"""

from __future__ import annotations

import math

import pytest

from custom_components.reolink_stamina.relevance.events import Event
from custom_components.reolink_stamina.relevance.rates import (
    CircularRate,
    build,
    duration_bucket,
    interpolate,
    lag_bucket,
    predecessor_label,
    quantile,
    recency,
)
from custom_components.reolink_stamina.relevance.score import (
    Score,
    calibrate,
    ready,
    score,
)

_CAMERA = "entry1:0"
_NOW = 1_800_000_000.0
_DAY = 86400.0


def _event(started_at: float, minute: int, kind: str, *, duration: float = 8.0) -> Event:
    """Build one event, with the solar term deliberately absent.

    Sunset would be a second reading of the same clock, and these tests are about whether the
    clock term works rather than about counting it twice.
    """
    return Event(
        camera=_CAMERA,
        kind=kind,
        started_at=started_at,
        ended_at=started_at + duration,
        duration=duration,
        minute_of_day=minute,
        solar_offset=None,
        is_weekend=False,
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


def _model():
    """Return a model trained on the boring household, already calibrated."""
    events = _history()
    model = build(events, now=_NOW)
    calibrate(model, events)
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
    calibrate(model, events)

    previous = None
    marked = []
    for event in events:
        if score(event, model, previous=previous).unusual:
            marked.append(event)
        previous = event

    assert len(marked) <= len(events) * 0.05
    assert all(event in marked for event in intruders)


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
