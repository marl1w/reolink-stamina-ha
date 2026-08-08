"""What each camera normally sees, learned by counting.

Two primitives carry the whole model.

A **circular rate** answers "how often does this camera see this at this time of day?". Not a
histogram of twenty-four hard bins — those are mostly empty on a quiet camera, and they make
23:59 and 23:01 different questions when they are plainly the same one. A kernel spreads each
event over its neighbours, wrapping at midnight, so the answer moves smoothly. The same
primitive serves clock time and minutes-from-sunset, which is the same shape of question
asked against a different clock.

A **categorical** answers "how often is it this?" for things with no order — which camera
fired before this one, how long it lasted once bucketed. Laplace smoothing, so a value seen
for the first time is surprising rather than infinitely so.

Both are recency-weighted with a ninety-day half-life, which is what lets a household change
without anyone retraining anything, and both are rebuilt from the journal rather than updated
in place. Rebuilding is a linear pass in plain Python — no numpy, and reads get *cheaper* as
history grows rather than more expensive, because scoring an event is a lookup either way.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import math

from ..const import (
    RATE_BACKOFF_WEIGHT,
    RATE_BANDWIDTH_MINUTES,
    RATE_BIN_MINUTES,
    RATE_BINS,
    RATE_FLOOR,
    RATE_HALF_LIFE_DAYS,
    RATE_SMOOTHING,
    SCORE_LAG_BUCKETS,
    SIGNAL_BAND_MIN,
    SIGNAL_BANDS,
)
from .events import Event

_SECONDS_PER_DAY = 86400.0


def _kernel(bandwidth: float) -> list[float]:
    """Return half a Gaussian kernel in bins, index 0 being the centre.

    Cut at two bandwidths, where the weight is already under a seventh of the peak and the
    arithmetic stops earning its keep.
    """
    reach = max(1, math.ceil(2.0 * bandwidth / RATE_BIN_MINUTES))
    return [
        math.exp(-0.5 * ((offset * RATE_BIN_MINUTES) / bandwidth) ** 2)
        for offset in range(reach + 1)
    ]


_KERNEL = _kernel(RATE_BANDWIDTH_MINUTES)


def recency(age_days: float, half_life: float = RATE_HALF_LIFE_DAYS) -> float:
    """Return how much an event that old still counts for."""
    return 0.5 ** (max(0.0, age_days) / half_life)


@dataclass(slots=True)
class CircularRate:
    """A smoothed distribution over the minutes of a day."""

    weights: list[float] = field(default_factory=lambda: [0.0] * RATE_BINS)
    counts: list[int] = field(default_factory=lambda: [0] * RATE_BINS)
    total: float = 0.0
    observations: int = 0

    def add(self, minute: int, weight: float) -> None:
        """Fold one observation in, spread across its neighbouring bins."""
        centre = (minute // RATE_BIN_MINUTES) % RATE_BINS
        for offset, share in enumerate(_KERNEL):
            for bin_index in {(centre + offset) % RATE_BINS, (centre - offset) % RATE_BINS}:
                self.weights[bin_index] += weight * share
        self.counts[centre] += 1
        self.total += weight * (2 * sum(_KERNEL) - _KERNEL[0])
        self.observations += 1

    def probability(self, minute: int) -> float:
        """Return the share of this camera's events that land near this minute.

        Floored rather than allowed to reach zero. A bin with nothing near it is the case
        this whole feature exists for, and it has to score heavily without scoring
        infinitely — one term at infinity would drown out every other.
        """
        if self.total <= 0.0:
            return 1.0 / RATE_BINS
        centre = (minute // RATE_BIN_MINUTES) % RATE_BINS
        return (self.weights[centre] + RATE_FLOOR) / (self.total + RATE_FLOOR * RATE_BINS)

    def nearby(self, minute: int) -> int:
        """Return how many events have actually landed within a bandwidth of this minute.

        Unweighted and unsmoothed, because this is the number that goes into the sentence
        shown to a person — "2nd time in six months" has to be a fact, not an estimate.
        """
        centre = (minute // RATE_BIN_MINUTES) % RATE_BINS
        reach = len(_KERNEL) - 1
        return sum(
            self.counts[(centre + offset) % RATE_BINS] for offset in range(-reach, reach + 1)
        )


@dataclass(slots=True)
class Categorical:
    """A smoothed distribution over labels with no order."""

    weights: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    total: float = 0.0
    observations: int = 0

    def add(self, label: str, weight: float) -> None:
        """Fold one observation in."""
        self.weights[label] = self.weights.get(label, 0.0) + weight
        self.counts[label] = self.counts.get(label, 0) + 1
        self.total += weight
        self.observations += 1

    def probability(self, label: str, *, categories: int) -> float:
        """Return the smoothed share of observations carrying this label."""
        seen = max(len(self.weights), categories, 1)
        return (self.weights.get(label, 0.0) + RATE_SMOOTHING) / (
            self.total + RATE_SMOOTHING * seen
        )

    def count(self, label: str) -> int:
        """Return how many times this label has actually been seen."""
        return self.counts.get(label, 0)


def duration_bucket(duration: float | None) -> str:
    """Return a coarse label for how long a detection lasted.

    Logarithmic, because the difference between two and four seconds matters and the
    difference between four and six minutes does not. Coarse on purpose: rarity is counted,
    and a continuous value never repeats, so it could never be rare.
    """
    if duration is None:
        return "open"
    if duration <= 0:
        return "instant"
    return f"~{2 ** int(math.log2(max(duration, 1.0)))}s"


def lag_bucket(seconds: float | None) -> str:
    """Return a coarse label for how long ago the previous camera fired."""
    if seconds is None:
        return "none"
    for edge in SCORE_LAG_BUCKETS:
        if seconds <= edge:
            return f"<{int(edge)}s"
    return "none"


@dataclass(slots=True)
class Profile:
    """Everything learned about one camera and kind."""

    clock: CircularRate = field(default_factory=CircularRate)
    solar: CircularRate = field(default_factory=CircularRate)
    duration: Categorical = field(default_factory=Categorical)
    predecessor: Categorical = field(default_factory=Categorical)
    # One distribution per configured signal, keyed by entity id. Added rather than
    # conditioned on: three booleans used as conditions would cut six months of history into
    # three weeks per bucket, while three more terms in a sum cost nothing in sample size.
    signals: dict[str, Categorical] = field(default_factory=dict)
    weight: float = 0.0
    events: int = 0
    first_seen: float | None = None
    last_seen: float | None = None

    @property
    def days(self) -> float:
        """Return how long this camera has been watched, in days."""
        if self.first_seen is None or self.last_seen is None:
            return 0.0
        return (self.last_seen - self.first_seen) / _SECONDS_PER_DAY


@dataclass(slots=True)
class Model:
    """The learned profiles, plus the fallbacks a thin one is blended with."""

    profiles: dict[tuple[str, str], Profile] = field(default_factory=dict)
    # What this subject does everywhere. The fallback a thin profile is blended towards, and
    # the choice matters more than it looks: backing off to what the *camera* sees would mix
    # subjects, so a person at one in the morning would be rescued by every cat that has ever
    # crossed at one in the morning. That is precisely the discrimination this exists for, so
    # the backoff holds the subject fixed and generalises the location instead.
    per_kind: dict[str, Profile] = field(default_factory=dict)
    # Kept for readiness and thresholds, which are per camera — not used for backoff.
    per_camera: dict[str, Profile] = field(default_factory=dict)
    overall: Profile = field(default_factory=Profile)
    built_at: float = 0.0
    # One threshold per camera, because surprisal is not on a portable scale. Empty for a
    # camera with too little behind it, which is what "still collecting" means.
    thresholds: dict[str, float] = field(default_factory=dict)
    # Cut points per numeric signal, learned from that sensor's own history. Empty for every
    # signal whose states were already categories.
    signal_bands: dict[str, tuple[float, ...]] = field(default_factory=dict)
    # The absolute minimum a score must clear, in nats, whatever the per-camera threshold says.
    #
    # Without one, a camera whose life is entirely predictable still marks its top few percent
    # — and on a real installation those thresholds were *negative*, so events more likely than
    # chance were being called unusual. A threshold answers "unusual for this camera"; the
    # floor answers "unusual at all", and a mark needs both to mean anything.
    floor: float = 0.0

    def profile(self, camera: str, kind: str) -> Profile:
        """Return what is known about this camera and kind, possibly nothing."""
        return self.profiles.get((camera, kind), Profile())

    def blended(self, camera: str, kind: str) -> tuple[Profile, Profile, Profile]:
        """Return the specific profile and the two it backs off to, in order."""
        return (
            self.profile(camera, kind),
            self.per_kind.get(kind, Profile()),
            self.overall,
        )


def interpolate(specific: float, backoff: float, weight: float) -> float:
    """Blend a sparse estimate towards a broader one.

    Jelinek-Mercer: the more evidence behind the specific estimate, the less the fallback is
    allowed to say. It is what stops a camera added last Tuesday declaring everything it sees
    remarkable, and it fades out by itself rather than at a threshold.
    """
    share = weight / (weight + RATE_BACKOFF_WEIGHT)
    return share * specific + (1.0 - share) * backoff


def predecessor_label(event: Event, previous: Event | None) -> str:
    """Describe what fired before this event, as a countable label.

    "Nothing" is a category rather than an absence, and it is the interesting one: a person
    at the back who never appeared at the front did not use the approach route. A gap too
    long to be the same subject walking counts as nothing too — a camera firing seven hours
    earlier did not precede anything.

    Public, and used by both the training pass and the scorer, because they must agree
    exactly. They did not, once: training recorded `camera|none` for a long gap while scoring
    looked up a bare `none`, so the commonest situation in a quiet household was never found
    in the table and every ordinary event scored as though it had never happened before.
    """
    if previous is None:
        return "none"
    lag = lag_bucket(event.started_at - previous.started_at)
    return "none" if lag == "none" else f"{previous.camera}|{lag}"


def numeric(raw: str) -> float | None:
    """Return a signal's value as a number, or None if it is not one.

    `unknown`, `unavailable` and every ordinary state land here as None, which is correct:
    they are categories in their own right and are counted as themselves.
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def bands(values: list[float], count: int = SIGNAL_BANDS) -> tuple[float, ...]:
    """Return the cut points that divide observed values into equal-sized groups.

    Learned from the history rather than fixed, and that is the whole reason numeric signals
    can be counted at all. A continuous value never repeats, so it can never be rare — and any
    fixed set of edges is wrong for every installation but one. Quantiles of what this sensor
    has actually read make "brighter than four days in five" a category with real counts
    behind it, on a camera under a porch light and on one facing a field.

    Nothing is returned where there is too little to cut, or where the sensor barely moves: a
    thermostat sitting at 21 all winter would otherwise produce five bands with one value in
    them, which is a lot of arithmetic to say nothing.
    """
    if len(values) < count * SIGNAL_BAND_MIN:
        return ()
    ordered = sorted(values)
    cuts = tuple(round(quantile(ordered, index / count), 4) for index in range(1, count))
    # Ties collapse the bands into each other; a sensor that reads the same number most of the
    # time is a category, not a range, and is counted as its own value.
    return cuts if len(set(cuts)) == len(cuts) else ()


def band_label(value: float, cuts: tuple[float, ...]) -> str:
    """Name the band a value falls in, as the range itself.

    The range rather than a word. "Above average" needs the reader to know the average, and
    invents a vocabulary that has to be explained; a number they can compare with the one on
    their own thermostat does not.
    """
    for index, cut in enumerate(cuts):
        if value < cut:
            return f"< {_trim(cut)}" if index == 0 else f"{_trim(cuts[index - 1])} to {_trim(cut)}"
    return f"{_trim(cuts[-1])} and up"


def _trim(value: float) -> str:
    """Render a cut point without a trailing zero nobody asked for."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:g}"


def signal_value(entity_id: str, raw: str, model: Model) -> str:
    """Return the label a signal's reading is counted as.

    Public, and used by the training pass and the scorer alike, because they must agree
    exactly — the same lesson as the predecessor label, which once recorded one string and
    looked up another so the commonest situation in a quiet household was never found.
    """
    cuts = model.signal_bands.get(entity_id)
    if not cuts:
        return raw
    reading = numeric(raw)
    return raw if reading is None else band_label(reading, cuts)


def build(events: list[Event], *, now: float) -> Model:
    """Learn from a history of events.

    A single linear pass. Events must arrive oldest first, which `derive` guarantees, because
    the predecessor term needs to know what came before each one.
    """
    model = Model(built_at=now)

    # One pass to learn each numeric signal's own scale, before anything is counted against
    # it. Categorical signals never reach this and are counted as they were recorded.
    readings: dict[str, list[float]] = {}
    for event in events:
        for entity_id, value in event.context:
            reading = numeric(value)
            if reading is not None:
                readings.setdefault(entity_id, []).append(reading)
    model.signal_bands = {
        entity_id: cuts for entity_id, values in readings.items() if (cuts := bands(values))
    }

    previous: Event | None = None
    for event in events:
        weight = recency((now - event.started_at) / _SECONDS_PER_DAY)
        label = predecessor_label(event, previous)
        bucket = duration_bucket(event.duration)

        for profile in (
            model.profiles.setdefault((event.camera, event.kind), Profile()),
            model.per_kind.setdefault(event.kind, Profile()),
            model.per_camera.setdefault(event.camera, Profile()),
            model.overall,
        ):
            profile.clock.add(event.minute_of_day, weight)
            if event.solar_offset is not None:
                profile.solar.add(event.solar_offset % 1440, weight)
            profile.duration.add(bucket, weight)
            profile.predecessor.add(label, weight)
            for entity_id, value in event.context:
                profile.signals.setdefault(entity_id, Categorical()).add(
                    signal_value(entity_id, value, model), weight
                )
            profile.weight += weight
            profile.events += 1
            if profile.first_seen is None:
                profile.first_seen = event.started_at
            profile.last_seen = event.started_at

        previous = event

    return model


def quantile(values: Iterable[float], share: float) -> float:
    """Return the value below which that share of the sample falls.

    Nearest-rank rather than interpolated: the sample is scores, the answer is a cut, and
    inventing a value between two real ones buys nothing.
    """
    ordered = sorted(values)
    if not ordered:
        return math.inf
    index = min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))
    return ordered[index]
