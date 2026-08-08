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

# Beta options. Both default to off, and with both off the integration behaves exactly as
# it did before they existed: nothing in the normal paths reads them.
#
# They are opt-in because each trades a guarantee this project otherwise keeps. Adaptive
# playback gives up "no ffmpeg, no subprocess, no CPU cost"; showing every device gives up
# "only hardware this has been tested against". Both are here to be reported on.
CONF_BETA_RESTREAM: Final = "beta_restream"
CONF_BETA_ALL_DEVICES: Final = "beta_all_devices"
# Relevance gives up "this integration keeps no record of your household". Nothing is
# journalled until it is switched on, which is why it is an option rather than a quiet
# default: see the journal module for the whole argument.
CONF_BETA_RELEVANCE: Final = "beta_relevance"

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
DEFAULT_BETA_RESTREAM: Final = False
DEFAULT_BETA_ALL_DEVICES: Final = False
DEFAULT_BETA_RELEVANCE: Final = False

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

# How long a camera must be quiet before its clip is considered finished. Several sensors
# fire within a second of each other on one arrival, and a person walks in and out of
# frame repeatedly; without this each would become its own upload of overlapping footage.
SYNC_SETTLE_SECONDS: Final = 20.0

# The recorder needs a moment before a new recording is findable at all. Measured on an
# RLN8-410: a detection at 19:50:10 became searchable 20 seconds later.
SYNC_WRITE_DELAY_SECONDS: Final = 20.0

# A detection sensor that sticks on would otherwise hold its clip open for ever, so a window
# is closed at this length and flagged as the first part of something longer.
SYNC_MAX_WINDOW_SECONDS: Final = 10 * 60.0

# A clip is assembled in memory on its way to the cloud, so this bounds what one event can
# cost. Comfortably above any sane lead+tail at full resolution.
SYNC_MAX_CLIP_BYTES: Final = 200 * 1024 * 1024

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
)

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

# A detection that clears and fires again inside this is one event, not two. Reolink sensors
# flap, and a person walks in and out of frame — counting each flicker separately would
# inflate the very rates this exists to keep honest. Cloud sync settles for twenty seconds
# before deciding a clip is finished, for the same reason and against the same hardware.
EVENT_MERGE_SECONDS: Final = 20.0

# A sensor stuck on would otherwise hold one event open for ever, and an event of unbounded
# length poisons the duration term for everything else on that camera.
EVENT_MAX_SECONDS: Final = 10 * 60.0

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
# thousand. No user-facing control while this is a beta; diagnostics reports the rate it
# actually produces, which is how it gets judged.
SCORE_QUANTILE: Final = 0.99

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
