"""How unusual an event is, and the sentence that says why.

Each signal contributes an independent surprisal and they are added together. It is naive
Bayes, the independence assumption is plainly false — an event after dark is more likely to
be at an odd hour — and it works perfectly well for ranking, which is all this is for.

What matters is that adding a signal does not *partition* the history. Conditioning on three
booleans would turn six months into three weeks per bucket and the estimates would collapse;
adding three terms in log space costs nothing in sample size. That is why the design has
terms rather than conditions, and why a new signal can arrive on its own later.

Every contribution is measured against a uniform baseline, so zero means "tells me nothing"
and the sum reads as a log-likelihood ratio rather than an arbitrary number. Positive is
rarer than chance, negative is more common — which makes a breakdown legible to a person
without explaining what a nat is.

The sentence is not decoration. A mark on a row that cannot say why is a machine asking to be
trusted, and this one has not earned that. If a change ever makes the sentence impossible to
generate, the model has become too clever for its own good.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..const import (
    RATE_BINS,
    SCORE_FLOOR,
    SCORE_MIN_DAYS,
    SCORE_MIN_EVENTS,
    SCORE_QUANTILE,
)
from .events import Event
from .rates import (
    Model,
    Profile,
    duration_bucket,
    interpolate,
    predecessor_label,
    quantile,
    signal_value,
)

# Below this a term is not worth mentioning in the sentence: it agrees with the model, and
# saying so would bury the one thing that does not.
_WORTH_SAYING = 0.5


@dataclass(slots=True, frozen=True)
class Term:
    """One signal's contribution, and what it was."""

    name: str
    # What this signal actually was, phrased for a person: "02:40", "4h after sunset".
    label: str
    # Positive means rarer than chance, negative means more common. Zero means uninformative.
    contribution: float
    # How many times this has been seen before. A fact for the sentence, not an estimate.
    seen: int
    # The signal's own name, where it has one — a chosen entity is called whatever Home
    # Assistant calls it, while "clock" and "duration" are named by the panel.
    subject: str = ""


@dataclass(slots=True, frozen=True)
class Score:
    """What the model makes of one event."""

    total: float
    # None while this camera is still collecting, which is not the same as zero.
    threshold: float | None
    unusual: bool
    terms: tuple[Term, ...]
    reason: str


def ready(profile: Profile) -> bool:
    """Whether a camera has enough behind it to be compared against itself."""
    return profile.events >= SCORE_MIN_EVENTS and profile.days >= SCORE_MIN_DAYS


def _surprisal(probability: float, categories: int) -> float:
    """Return how much rarer than chance an observation is, in nats."""
    if probability <= 0.0:
        probability = 1e-9
    return -math.log(probability * max(categories, 1))


def _clock_time(minute: int) -> str:
    """Render a minute of the day as a clock time."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


# What each phase is called in a sentence. The model still counts minutes from sunset — a
# continuous, seasonal number is what makes the term worth having — but nobody reads a number
# measured from an event nine hours away, and "8h 53m before sunset" for a lunchtime detection
# is how a correct model comes to look broken.
_SOLAR_PHRASES = {
    "dawn": "around sunrise",
    "day": "in daylight",
    "dusk": "around sunset",
    "night": "after dark",
}


def _solar_phrase(phase: str | None, offset: int) -> str:
    """Render where the sun was as something a person would say."""
    named = _SOLAR_PHRASES.get(phase or "")
    if named is not None:
        return named
    # No location configured, or a latitude where the sun does not rise or set that day. The
    # offset is all there is, so it is what gets said.
    if offset == 0:
        return "at sunset"
    hours, minutes = divmod(abs(offset), 60)
    if hours and minutes:
        span = f"{hours}h {minutes}m"
    elif hours:
        span = f"{hours}h"
    else:
        span = f"{minutes}m"
    return f"{span} {'after' if offset > 0 else 'before'} sunset"


def _ordinal(number: int) -> str:
    """Render a small counting number as 1st, 2nd, 3rd."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _span(days: float) -> str:
    """Render how long a camera has been watched, roughly and readably."""
    if days >= 60:
        return f"{round(days / 30)} months"
    if days >= 14:
        return f"{round(days / 7)} weeks"
    return f"{max(1, round(days))} days"


def _terms(
    event: Event,
    previous: Event | None,
    model: Model,
    names: dict[str, str],
    labels: dict[str, str] | None = None,
) -> list[Term]:
    """Return every signal's contribution to how unusual this event is."""
    labels = labels or {}
    specific, broader, overall = model.blended(event.camera, event.kind)

    def blend(pick) -> float:
        """Fall back from this camera and kind, to the kind anywhere, to everything."""
        return interpolate(
            pick(specific),
            interpolate(pick(broader), pick(overall), broader.weight),
            specific.weight,
        )

    terms = [
        Term(
            name="clock",
            label=_clock_time(event.minute_of_day),
            contribution=_surprisal(
                blend(lambda p: p.clock.probability(event.minute_of_day)), RATE_BINS
            ),
            seen=specific.clock.nearby(event.minute_of_day),
        )
    ]

    if event.solar_offset is not None:
        offset = event.solar_offset
        terms.append(
            Term(
                name="solar",
                label=_solar_phrase(event.solar_phase, offset),
                contribution=_surprisal(
                    blend(lambda p: p.solar.probability(offset % 1440)), RATE_BINS
                ),
                seen=specific.solar.nearby(offset % 1440),
            )
        )

    bucket = duration_bucket(event.duration)
    categories = max(len(specific.duration.weights), len(overall.duration.weights), 2)
    terms.append(
        Term(
            name="duration",
            label=bucket,
            contribution=_surprisal(
                blend(lambda p: p.duration.probability(bucket, categories=categories)),
                categories,
            ),
            seen=specific.duration.count(bucket),
        )
    )

    # What the other cameras were doing. The absent case is the interesting one: something at
    # the back that never appeared at the front did not use the approach route.
    label = predecessor_label(event, previous)
    if label == "none":
        phrase = "nothing fired first"
    else:
        phrase = f"after {names.get(previous.camera, 'another camera')}"
    categories = max(len(specific.predecessor.weights), len(overall.predecessor.weights), 2)
    terms.append(
        Term(
            name="predecessor",
            label=phrase,
            contribution=_surprisal(
                blend(lambda p: p.predecessor.probability(label, categories=categories)),
                categories,
            ),
            seen=specific.predecessor.count(label),
        )
    )

    # Whatever else the user asked to be counted. Absent is a value in its own right rather
    # than a gap: a signal added six months in was genuinely unknown before that, and
    # silently skipping those events would shift every count that mentions it.
    seen = dict(event.context)
    for entity_id in sorted(set(specific.signals) | set(seen)):
        # Through the same function the training pass used. A numeric signal is counted as the
        # band its reading fell in, and training recording "12 to 18" while scoring looked up
        # "14.2" would mean no numeric signal ever matched anything it had learned.
        value = signal_value(entity_id, seen.get(entity_id, "unknown"), model)
        distribution = specific.signals.get(entity_id)
        categories = max(len(distribution.weights) if distribution else 0, 2)
        terms.append(
            Term(
                name="signal",
                subject=labels.get(entity_id, entity_id),
                label=value,
                contribution=_surprisal(
                    # Bound as defaults, not captured: a lambda built in a loop that reads
                    # the loop variable sees whatever it holds when the lambda finally runs,
                    # which here is the last signal for every one of them.
                    blend(
                        lambda p, key=entity_id, was=value, many=categories: (
                            p.signals[key].probability(was, categories=many)
                            if key in p.signals
                            else 1.0 / many
                        )
                    ),
                    categories,
                ),
                seen=distribution.count(value) if distribution else 0,
            )
        )

    return terms


def total(event: Event, previous: Event | None, model: Model) -> float:
    """Return only the number.

    Calibration walks the entire history on every rebuild and has no use for the sentences,
    which cost more to assemble than the arithmetic they describe.
    """
    return sum(term.contribution for term in _terms(event, previous, model, {}))


def score(
    event: Event,
    model: Model,
    *,
    previous: Event | None,
    names: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> Score:
    """Return how unusual this event is, and the sentence that says why.

    `previous` is the event before this one across every camera, and it is required rather
    than defaulted for a reason worth stating: the threshold is calibrated over a particular
    set of terms, so scoring against a *different* set compares two things measured on
    different scales, and nothing would ever be marked. Making the caller say `None` — which
    means "nothing fired first", a real and telling signal — is what stops that being a
    mistake anyone can make quietly.
    """
    names = names or {}
    terms = _terms(event, previous, model, names, labels)
    summed = sum(term.contribution for term in terms)
    threshold = model.thresholds.get(event.camera)
    # The stricter of the two, and reported as such: the panel draws the line it was actually
    # measured against, so a gauge showing a score just past its camera's threshold but short
    # of the floor does not look like a mark that failed to appear.
    against = None if threshold is None else max(threshold, model.floor)
    return Score(
        total=summed,
        threshold=against,
        unusual=against is not None and summed > against,
        terms=tuple(terms),
        reason=_reason(event, terms, model.profile(event.camera, event.kind), names),
    )


def _reason(event: Event, terms: list[Term], profile: Profile, names: dict[str, str]) -> str:
    """Build the sentence a person reads instead of the numbers.

    The largest one or two contributions, and a count that is a fact rather than an estimate.
    Nothing here is generated from the score itself: if the model cannot say which signal made
    an event stand out, there is no sentence to show and none is invented.
    """
    subject = names.get(event.camera, "This camera")
    kind = event.kind.replace("_", " ").capitalize()

    ranked = sorted(terms, key=lambda term: term.contribution, reverse=True)
    leading = [term for term in ranked if term.contribution >= _WORTH_SAYING][:2]
    if not leading:
        return f"{kind} at {_clock_time(event.minute_of_day)} — usual for {subject}."

    head = leading[0]
    opening = f"{kind} {_phrase(head)}"

    # The count includes this one, which is how a person counts.
    occurrence = _ordinal(head.seen + 1)
    span = _span(profile.days) if profile.days else "the record so far"
    sentence = f"{opening} on {subject} — {occurrence} time in {span}"

    if len(leading) > 1:
        sentence += f", and {_phrase(leading[1])}"
    return f"{sentence}."


def _phrase(term: Term) -> str:
    """Render one signal as part of a sentence rather than as a label.

    The labels are written for the breakdown, where each sits in its own row and needs no
    grammar. Dropped into prose they read badly — "Person with after Gate", "and 04:41" —
    and the sentence is the whole feature's credibility, so each kind of signal gets the
    words that make it a phrase.
    """
    if term.name == "clock":
        return f"at {term.label}"
    if term.name == "solar":
        return term.label
    if term.name == "duration":
        return f"lasting {term.label}"
    if term.name == "predecessor":
        # Already a phrase in both of its forms: "nothing fired first", "after Gate".
        return term.label
    if term.name == "signal" and term.subject:
        # A chosen signal has a name of its own, and the state alone is meaningless in a
        # sentence: "with armed_away" says nothing about what was armed.
        return f"with {term.subject} {term.label}"
    return f"with {term.label}"


def calibrate(
    model: Model,
    events: list[Event],
    *,
    share: float = SCORE_QUANTILE,
    floor: float = SCORE_FLOOR,
) -> None:
    """Work out how high a score has to be before it is worth marking.

    Two numbers, and an event has to clear both.

    The *quantile* is per camera, over that camera's own history, because surprisal is not on
    a portable scale: the same figure means different things on a camera with two hundred
    events and one with twenty thousand. It stops a chatty camera flooding the list.

    The *floor* is absolute, and it is the one that says a mark means something. Measured on a
    real installation — nine cameras, a fortnight, 5,659 events — the per-camera thresholds at
    the 0.95 quantile ran from -0.63 to 0.45, median 0.05. A negative threshold marks events
    that were *more* likely than chance, which is how a person seen for the seventh time in
    ten days came to be called unusual. Only 4.6% of all events scored above zero at all.

    Cameras without enough behind them get no threshold at all, which is what the panel reads
    as "still collecting" — deliberately not the same as a threshold of zero.
    """
    scores: dict[str, list[float]] = {}
    previous: Event | None = None
    for event in events:
        scores.setdefault(event.camera, []).append(total(event, previous, model))
        previous = event

    model.thresholds = {
        camera: quantile(values, share)
        for camera, values in scores.items()
        if ready(model.per_camera.get(camera, Profile()))
    }
    model.floor = floor
