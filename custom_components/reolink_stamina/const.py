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

# Beta options. Both default to off, and with both off the integration behaves exactly as
# it did before they existed: nothing in the normal paths reads them.
#
# They are opt-in because each trades a guarantee this project otherwise keeps. Adaptive
# playback gives up "no ffmpeg, no subprocess, no CPU cost"; showing every device gives up
# "only hardware this has been tested against". Both are here to be reported on.
CONF_BETA_RESTREAM: Final = "beta_restream"
CONF_BETA_ALL_DEVICES: Final = "beta_all_devices"

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
DEFAULT_BETA_RESTREAM: Final = False
DEFAULT_BETA_ALL_DEVICES: Final = False

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
