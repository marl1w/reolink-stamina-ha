"""Constants for the Reolink Stamina integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "reolink_stamina"
REOLINK_DOMAIN: Final = "reolink"

# Sidebar panel
PANEL_URL_PATH: Final = "reolink-events"
PANEL_TITLE: Final = "Reolink Stamina"
PANEL_ICON: Final = "mdi:motion-play-outline"
PANEL_COMPONENT: Final = "reolink-stamina-panel"
STATIC_URL: Final = "/reolink_stamina_static"

# Options
CONF_BROWSE_STREAM: Final = "browse_stream"
CONF_SPLIT_MINUTES: Final = "split_minutes"
CONF_HIDE_TIMER: Final = "hide_timer"
CONF_PRE_ROLL: Final = "pre_roll"
CONF_REQUIRE_ADMIN: Final = "require_admin"
CONF_INCLUDE_UNLABELLED: Final = "include_unlabelled"
CONF_EVENT_LEAD: Final = "event_lead"
CONF_CLIP_LEAD: Final = "clip_lead"
CONF_CLIP_TAIL: Final = "clip_tail"
# Whether the recorder's TLS certificate is checked. See `tls.py` for why the answer is no
# unless asked: recorders ship a self-signed certificate, and reolink_aio — which every
# other call to the device goes through — never checks it either.
CONF_VERIFY_TLS: Final = "verify_tls"

DEFAULT_BROWSE_STREAM: Final = "sub"
DEFAULT_SPLIT_MINUTES: Final = 5
DEFAULT_HIDE_TIMER: Final = True
DEFAULT_PRE_ROLL: Final = 5
DEFAULT_REQUIRE_ADMIN: Final = True
# Continuous recording arrives with no trigger flag at all and outnumbers real
# detections by roughly 30:1, so it is discarded before it is ever stored or sent.
DEFAULT_INCLUDE_UNLABELLED: Final = False
# Seconds before a detection to start playing. A five-minute segment is tedious to
# watch for a few seconds of event, and seeking is server-side, so playback can simply
# open at the right place.
DEFAULT_EVENT_LEAD: Final = 30
# How far either side of the detections a clip extends on 24/7 footage, which is what
# turns a fixed five-minute segment into a clip the length of what happened. Separate
# from the lead above: these bound the clip, that one only places the playhead inside it.
# Where a camera records on events the recorder has already done this, and its own
# pre-record buffer must not be trimmed away — so neither applies there.
DEFAULT_CLIP_LEAD: Final = 15
DEFAULT_CLIP_TAIL: Final = 15
# Off, because a Reolink recorder's factory certificate cannot pass verification and the
# calls this integration does not make itself are already not verifying it.
DEFAULT_VERIFY_TLS: Final = False

STREAM_MAIN: Final = "main"
STREAM_SUB: Final = "sub"

# How far back the Reolink search API is willing to look.
SEARCH_WINDOW_DAYS: Final = 30

# Cache freshness. A day in the past can no longer gain recordings, so it is
# cached aggressively; today is still being written to and expires quickly.
# Today is still being written to, but a 60s window meant every reselection
# re-searched every camera. Five minutes is well inside how fast recordings appear,
# and the refresh button forces an update when it matters.
TTL_TODAY: Final = 300.0
TTL_PAST: Final = 7 * 24 * 3600.0

# Searching a device is expensive for the device itself; never hammer it.
MAX_CONCURRENT_SEARCHES: Final = 2

# A camera recording continuously covers essentially all of the day, so anything it
# reports without a trigger is filler. A camera recording on events covers almost none of
# it, and its unlabelled recordings *are* the events. Measured on real recorders the two
# are not close — 0.8% against 115% — so the threshold only has to be somewhere sensible.
CONTINUOUS_COVERAGE: Final = 0.7

# Shape of a serialised recording. Bump this whenever a field is added or changed:
# cached records written by an older version are then treated as stale and refetched,
# instead of silently lacking the new field. Playback needs playback_id, and its absence
# from records cached before it existed made every clip unplayable.
FILE_SCHEMA_VERSION: Final = 7

STORAGE_KEY: Final = f"{DOMAIN}.cache"
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 20.0

# Triggers that represent "activity" rather than a schedule. Used for the
# default filter offered by the panel.
ACTIVITY_TRIGGERS: Final = (
    "person",
    "vehicle",
    "animal",
    "doorbell",
    "package",
    "face",
    "crying",
    "crossline",
    "intrusion",
    "linger",
    "forgotten_item",
    "taken_item",
    "io",
)

ISSUE_INCOMPATIBLE: Final = "reolink_incompatible"

# --------------------------------------------------------------------- cloud sync

# Each recorder that syncs to the cloud is a subentry of the panel's own config entry: the
# panel is one thing, and a syncer per NVR is several, each with its own switch, quota and
# destination. One subentry per NVR, so a sync device is the counterpart of the recorder
# device the Reolink integration creates.
SUBENTRY_TYPE_SYNC: Final = "cloud_sync"

CONF_NVR_ENTRY: Final = "nvr_entry_id"
CONF_DESTINATION: Final = "destination"
CONF_DESTINATION_ENTRY: Final = "destination_entry_id"
CONF_QUOTA_GB: Final = "quota_gb"
CONF_SYNC_KINDS: Final = "kinds"
CONF_SYNC_LEAD: Final = "sync_lead"
CONF_SYNC_TAIL: Final = "sync_tail"
CONF_SYNC_STREAM: Final = "sync_stream"
CONF_REMOTE_FOLDER: Final = "remote_folder"
# A second admission rule, alongside the kinds above: upload anything the model marked as
# unusual for its camera, whatever kind it was. The two are ORed, so a recorder can sync
# people always and everything else only when it is out of the ordinary.
CONF_SYNC_UNUSUAL: Final = "sync_unusual"
CONF_SYNC_UNUSUAL_KINDS: Final = "unusual_kinds"

DEFAULT_QUOTA_GB: Final = 15

# Decimal, not binary, and it has to stay that way: the form is labelled GB, and the quota
# sensors report bytes with `UnitOfInformation.GIGABYTES`, which Home Assistant converts by
# 10**9. Multiplying the entered number by 1024**3 made a "15 GB" quota hold 16.11 GB and
# display 16.11 as its free space — three places, two definitions of a gigabyte.
BYTES_PER_GB: Final = 1000**3
# The recorder's own detections, minus plain motion: on a 24/7 camera motion fires
# constantly and would spend the whole quota on empty footage.
DEFAULT_SYNC_KINDS: Final = ("person", "vehicle", "animal")
DEFAULT_SYNC_LEAD: Final = 10
DEFAULT_SYNC_TAIL: Final = 10
DEFAULT_REMOTE_FOLDER: Final = "reolink"
# Off, and while it is off nothing is scored and no extra sensor is watched.
DEFAULT_SYNC_UNUSUAL: Final = False

# Every detection type a Reolink camera can report, in the panel's own vocabulary. Offered
# as the choice of what to sync; a camera simply never fires the ones it lacks.
SYNC_KIND_CHOICES: Final = (
    "person",
    "vehicle",
    "animal",
    "package",
    "face",
    "doorbell",
    "crying",
    "motion",
)

# Which kinds the unusual rule may upload, when nothing is chosen. All of them: the point of
# the rule is the kind you did *not* think worth syncing turning out to be worth seeing.
DEFAULT_SYNC_UNUSUAL_KINDS: Final = SYNC_KIND_CHOICES

# How long a camera must be quiet before its clip is considered finished. Several sensors
# fire within a second of each other on one arrival, and a person walks in and out of
# frame repeatedly; without this each would become its own upload of overlapping footage.
#
# Fifteen seconds, and the number comes from the same measurement the relevance merge window
# was chosen from: across 5,187 detections on real hardware, the quiet gap between one
# detection ending and the next starting was 3.2s at the first quartile and 10.5s at the
# median. So this comfortably absorbs sensor bounce and somebody stepping out of frame,
# while the twenty seconds it started at was buying very little and every clip paid it — it
# is the largest single term in how long an upload takes to appear.
SYNC_SETTLE_SECONDS: Final = 15.0

# The recorder needs a moment before a new recording is findable at all. Measured on an
# RLN8-410: a detection at 19:50:10 became searchable 20 seconds later.
#
# Measured from the *detection*, which is what it is applied to: waiting this out from the
# padded end of the window instead meant a recorder with a sixty-second tail waited eighty
# seconds for a file that was findable after twenty.
SYNC_WRITE_DELAY_SECONDS: Final = 20.0

# How the search for a settled recording is paced. Short looks first, because the common case
# is a file that is already there and already long enough — a flat ten-second poll charged
# every clip for the worst case. The total budget is unchanged.
SYNC_SEARCH_BACKOFF: Final = (
    2.0,
    3.0,
    5.0,
    8.0,
    *(10.0,) * 10,
)

# Two looks have to be this far apart before "the file stopped growing" means anything. The
# exact test — the recording already reaches the end of the window — needs no such spacing
# and is what usually ends the search; this only governs the fallback for a recording that
# ended early, where two samples two seconds apart would call a live file finished.
SYNC_STABLE_SPACING_SECONDS: Final = 10.0

# A detection sensor that sticks on would otherwise hold its clip open for ever, so a window
# is closed at this length and flagged as the first part of something longer.
SYNC_MAX_WINDOW_SECONDS: Final = 10 * 60.0

# A clip is assembled in memory on its way to the cloud, so this bounds what one event can
# cost. Comfortably above any sane lead+tail at full resolution.
SYNC_MAX_CLIP_BYTES: Final = 200 * 1024 * 1024

# ------------------------------------------------------- how much to do at once
#
# One person crossing three cameras is three clips on one recorder, and handling them strictly
# one after another means the third waits out the first two entirely. They are overlapped
# instead — but only as far as the machine can actually take, because a clip is held in memory
# from the moment it is fetched until the upload finishes. Memory is the constraint that
# matters: overlapping is precisely what makes a slow link go faster, while a Pi that runs out
# of memory takes Home Assistant with it. See `cloud/capacity.py`.

# What share of the memory the machine has free may be spent on clips in flight.
SYNC_MEMORY_SHARE: Final = 0.25
# And never more than this, however much the machine has. Past a handful of clips the recorder
# lock and the link are the limits anyway, so a bigger budget buys nothing.
SYNC_MEMORY_CEILING: Final = 512 * 1024 * 1024
# Never more than this many at once whatever the arithmetic says. A continuously recording
# camera's clip is cut by an ffmpeg of its own, so this is a CPU bound as much as anything.
SYNC_MAX_CONCURRENT_CLIPS: Final = 4
# What one clip is assumed to cost before any have been measured. Deliberately pessimistic —
# roughly a minute of main stream — so a machine earns its concurrency by uploading small
# clips rather than being granted it and finding out.
SYNC_ASSUMED_CLIP_BYTES: Final = 32 * 1024 * 1024
# How many recent clips the estimate is taken from. The largest of them, not the average: the
# budget has to hold the next clip, and clips vary by an order of magnitude between a doorbell
# press and a car manoeuvring.
SYNC_CLIP_SAMPLES: Final = 10

# --------------------------------------------------------------------- relevance

# Its own database rather than a corner of Home Assistant's: the recorder's file belongs to
# the recorder, and this one has to outlive the recorder's retention to be worth keeping.
JOURNAL_FILENAME: Final = f"{DOMAIN}_journal.db"

# Bumped whenever `_MIGRATIONS` in the journal gains an entry. Never reused, never reordered:
# somebody's year of history is migrated by replaying every step above the number written in
# their file.
JOURNAL_SCHEMA_VERSION: Final = 1

# Transitions are buffered rather than committed one at a time. A busy recorder fires several
# sensors within a second of each other, and an fsync per sensor would put the journal in the
# way of everything else the machine is doing. The cost is the last few seconds of transitions
# on an unclean shutdown, which for a statistical record is nothing.
JOURNAL_FLUSH_SECONDS: Final = 30.0
JOURNAL_FLUSH_ROWS: Final = 64

# How much of the recorder's history to import when the feature is switched on. The recorder
# is asked what it actually keeps; these only bound the answer, because `purge_keep_days` can
# be set to anything and a first-run import is not the place to read years of history.
JOURNAL_BACKFILL_DAYS_DEFAULT: Final = 10
JOURNAL_BACKFILL_DAYS_MAX: Final = 90

# History is read a day at a time. One query for every sensor over ninety days is a heavy hit
# on a database that is also serving the rest of Home Assistant, and the import must never be
# the reason a restart feels slow.
JOURNAL_BACKFILL_CHUNK_HOURS: Final = 24

# Set once the initial import has run, so it does not repeat on every restart.
JOURNAL_META_BACKFILLED: Final = "backfilled_at"
# The signal entities whose history has been stamped onto the transitions already held. The
# value is the set itself rather than a flag, because adding one signal has to re-stamp every
# row: a snapshot missing an entity and one recording it as absent are different facts.
JOURNAL_META_SIGNALS: Final = "signals_backfilled"
JOURNAL_META_SCHEMA: Final = "schema_version"

# Entities whose state is snapshotted onto every detection, as {reolink_entry_id: [entity]}.
# Scoped per recorder rather than per installation: one Home Assistant often serves more than
# one property, and "is anyone home" at one of them says nothing about the other.
CONF_RELEVANCE_SIGNALS: Final = "relevance_signals"

# What the picker offers. Every one of these has a state a person could read out loud, which
# is what makes it countable — a continuous `sensor` never repeats a value, so it could never
# be rare, and it needs bucketing before it can be offered at all.
RELEVANCE_SIGNAL_DOMAINS: Final = (
    "person",
    "device_tracker",
    "binary_sensor",
    "alarm_control_panel",
    "input_boolean",
    "input_select",
    "sun",
    "calendar",
    # A camera's own floodlight and siren, and the gates and door locks around it. All of
    # them say something a detection cannot: a person on the drive with the gate unlocked is
    # an arrival, and the same person with it locked is somebody who climbed over.
    "light",
    "lock",
    "siren",
)

# One more domain, admitted only for the entities in it that hold a fixed set of values.
# `sensor` as a whole is thousands of numbers, and a number this model cannot bucket is a term
# that never repeats; an enum sensor is a category by construction, which is exactly the shape
# the counting wants. Reolink's own "Day night state" is one, and it is the best signal on the
# camera: it knows whether the picture is infrared, which is darkness as the camera sees it
# rather than as an almanac calculates it.
RELEVANCE_SIGNAL_ENUM_DOMAINS: Final = ("sensor",)

# Numeric sensors are admitted too, but only the ones measuring the world rather than the
# wiring. On the installation this was measured against, "any sensor with a unit" offered 383
# entities and all but a handful were voltage, current and energy counters; this list offers
# 85, and they are the weather station and the room sensors.
RELEVANCE_SIGNAL_WORLD_CLASSES: Final = (
    "aqi",
    "atmospheric_pressure",
    "distance",
    "humidity",
    "illuminance",
    "irradiance",
    "moisture",
    "pm25",
    "precipitation",
    "precipitation_intensity",
    "pressure",
    "sound_pressure",
    "speed",
    "temperature",
    "uv_index",
    "wind_speed",
)

# How many equal-population bands a numeric signal is cut into, and how many readings each
# band needs before cutting is worth doing at all. Five is enough to separate "unusually dark"
# from "ordinary" without asking a few hundred events to fill twenty buckets.
SIGNAL_BANDS: Final = 5
SIGNAL_BAND_MIN: Final = 20

# What a camera reports about itself, counted beside its own detections without anybody
# choosing it — discovered exactly as the detection sensors are. `(domain, reolink key)`,
# because the key alone is ambiguous and the domain alone is far too broad.
CAMERA_SIGNAL_KEYS: Final = frozenset(
    {
        ("light", "floodlight"),
        ("siren", "siren"),
        ("sensor", "day_night_state"),
    }
)

CONF_RELEVANCE_SENSITIVITY: Final = "relevance_sensitivity"
# How rare an event has to be before it is marked, offered as three words rather than a number.
#
# The word chooses a *floor*, in nats, and not the quantile — which is the opposite of how this
# started, and the measurement is why. On a real installation of nine cameras over a fortnight,
# 5,659 events: with a floor in place, moving the quantile from 0.90 to 0.99 changed the number
# of marks from 54 to 44. The quantile had stopped being the thing that decided.
#
# A floor is also the more honest control. It says "at least this much rarer than chance",
# which is a statement about the event; a quantile says "the top few percent of this camera",
# which marks something however ordinary a week has been.
#
#   0.7 nats — twice as rare as chance      measured: 125 marks, ~9 a day
#   1.4 nats — four times                   measured:  54 marks, ~4 a day
#   2.1 nats — eight times                  measured:  23 marks, ~1.6 a day
RELEVANCE_SENSITIVITY_FLOORS: Final = {
    "few": 2.1,
    "balanced": 1.4,
    "many": 0.7,
}
DEFAULT_RELEVANCE_SENSITIVITY: Final = "balanced"

# A signal with more distinct values than this is noise wearing a signal's clothes: with six
# months of history behind it, forty categories hold a handful of events each.
RELEVANCE_SIGNAL_MAX_VALUES: Final = 12

JOURNAL_SOURCE_LIVE: Final = "live"
JOURNAL_SOURCE_BACKFILL: Final = "backfill"

# ---------------------------------------------------------- deriving events
#
# Every number below is applied when transitions are read, never when they are written, so
# each is a decision that can be changed later and re-applied to history already collected.
# They are first guesses; `scripts/replay.py` is how they get chosen properly.

# A detection that clears and fires again inside this is one event, not two: Reolink sensors
# bounce, and counting each flicker separately would inflate the very rates this exists to
# keep honest.
#
# Three seconds, measured rather than guessed. Across 5,187 detections on real hardware the
# quiet gap between one ending and the next starting was 3.2s at the first quartile and 10.5s
# at the median — so genuine bounce is under three seconds and anything longer is a separate
# thing happening. The twenty seconds this started at was borrowed from cloud sync, where a
# generous window only costs a slightly long clip; here it merged 63% of all gaps, and since
# each merge extends the run rather than closing it, a car manoeuvring became one event that
# ran until it hit the cap below. That is where the two-hour vehicle detections came from,
# and why a row saying "Person (4)" opened a sheet offering two.
EVENT_MERGE_SECONDS: Final = 3.0

# A sensor stuck on would otherwise hold one event open for ever, and an event of unbounded
# length poisons the duration term for everything else on that camera. The longest any sensor
# was genuinely on across those ten days was 81 seconds, so this is a guard against something
# broken rather than a bound on anything real.
EVENT_MAX_SECONDS: Final = 5 * 60.0

# --------------------------------------------------------------- rate tables
#
# Five-minute bins over the day. Fine enough that a school run and an evening return do not
# land in the same bucket, coarse enough that a year of events still fills it.
RATE_BINS: Final = 288
RATE_BIN_MINUTES: Final = 1440 // RATE_BINS

# Smoothing, in minutes. Hard bins would call 23:59 rare while 23:01 is common; this is the
# width of the kernel that stops them being different questions.
RATE_BANDWIDTH_MINUTES: Final = 45.0

# How fast the past stops counting. Ninety days means a household that changes — a new job, a
# new season, a child — is tracked without anyone retraining anything, and last winter still
# carries a little weight this winter.
RATE_HALF_LIFE_DAYS: Final = 90.0

# Below this much evidence, a camera-and-kind pair is blended with the camera's own marginal
# and then with everything seen anywhere. Standard interpolation, and it is what stops a newly
# added camera declaring everything it sees to be remarkable.
RATE_BACKOFF_WEIGHT: Final = 40.0

# Laplace smoothing for the categorical terms, so a value seen for the first time is
# surprising rather than infinitely surprising.
RATE_SMOOTHING: Final = 0.5

# The same idea for the smooth distributions, and deliberately much smaller. A bin with
# nothing anywhere near it is the "person at three in the morning" case and should score
# heavily — but finitely, because an infinite term would swamp every other. Being small also
# makes "never" more surprising the longer a camera has been watched, which is right: never
# in a fortnight is thin evidence, never in a year is not.
RATE_FLOOR: Final = 0.05

# ------------------------------------------------------------------ scoring

# Where the predecessor term stops looking. Beyond a couple of minutes, one camera firing
# after another is a coincidence rather than the same subject walking.
SCORE_LAG_BUCKETS: Final = (10.0, 30.0, 120.0)

# How rare an event has to be before it is worth marking. A quantile of that camera's own
# scores rather than an absolute number, because surprisal is not on a portable scale: the
# same figure means different things on a camera with two hundred events and one with twenty
# thousand.
#
# One in twenty, not one in a hundred. At 0.99 a real installation marked three events in a
# month and every one of them was a person, and a test household marked nothing but the very
# oddest thing on each camera. Measured against planted anomalies — a vehicle at three in the
# morning, a doorbell at four — those land around the top four per cent, not the top one: odd
# enough that somebody would want to see them, not so odd that nothing else comes close.
#
# It is the honest lever here. Combining the terms differently was tried first and moved
# nothing: summed, positives only, strongest alone, strongest plus a share of the rest all
# ranked those anomalies within a place or two of each other. What decides whether they are
# marked is where the line sits, not how the number is built.
SCORE_QUANTILE: Final = 0.95

# The default floor, in nats, matching "balanced" above. Four times rarer than chance.
SCORE_FLOOR: Final = 1.4

# Nothing is scored until a camera has this much behind it. Before that the panel says it is
# still collecting, which is true and better than being wrong.
#
# Seven days rather than fourteen, and the reason is a real installation rather than a guess:
# a camera there had 493 detections after ten days — two and a half times the count it needed
# — and was still being held back by the calendar alone. What the days are actually for is
# having seen each day of the week once, because a household's Saturday does not look like
# its Tuesday. That is one week, not two.
SCORE_MIN_DAYS: Final = 7.0
SCORE_MIN_EVENTS: Final = 200
