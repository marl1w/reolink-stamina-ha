#!/usr/bin/env python3
"""Serve the real panel on this machine, against invented data.

The panel is a graph of ES modules that talks to Home Assistant through exactly two things:
`hass.callWS` and `hass.connection.subscribeMessage`. Give it stand-ins for those and it runs
anywhere — so this serves the actual files out of `custom_components/reolink_stamina/frontend`
and answers their websocket commands from a made-up household.

    scripts/preview.py                 # http://127.0.0.1:8123
    scripts/preview.py --port 9000
    scripts/preview.py --scenario new  # a camera on the day it was switched on

What this is good for: seeing a change without restarting Home Assistant, and looking at
states that are tedious to arrange on real hardware — a camera that is still collecting, one
with an unusual event in it, a row nothing was marked on.

The player works too, on a test pattern ffmpeg makes on first run rather than on footage. It
is served with Range support and a running counter in the picture, so seeking to 2:30 and
seeing 2:30 says the scrub bar works rather than merely that the player is on screen. Without
ffmpeg the rows report themselves unplayable, which is a state worth looking at as well.

The relevance figures are not invented. The sample detections are fed through the real
`relevance` engine — the same merging, the same rate tables, the same scoring — so what the
marks and the numbers do here is what they would do on that data in Home Assistant.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import UTC, datetime, timedelta
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "custom_components" / "reolink_stamina" / "frontend"
sys.path.insert(0, str(ROOT))

from custom_components.reolink_stamina.relevance.events import Event  # noqa: E402
from custom_components.reolink_stamina.relevance.rates import build  # noqa: E402
from custom_components.reolink_stamina.relevance.score import (  # noqa: E402
    SCORE_MIN_DAYS,
    SCORE_MIN_EVENTS,
    calibrate,
    ready,
    score,
)

# Everything, by default.
#
# `make preview` with no arguments has to be worth trusting on its own, which means the
# household it invents has to contain every state the panel can be in — not one state per
# flag, because a state behind a flag is a state nobody looks at. So: two working recorders
# and one that is offline, cameras that are learned and cameras switched on yesterday, every
# kind of detection the panel colours, footage that is continuous and footage that is not,
# rows that play and rows that cannot.

RECORDERS = (
    # entry id, name, model, status
    ("01VILLA", "Villa NVR", "RLN8-410", "ok"),
    ("01GARAGE", "Garage NVR", "RLN16-410", "ok"),
    # Greyed out in the picker, with the card saying why. A state that only appears when
    # somebody's recorder is genuinely unplugged is one that never gets looked at otherwise.
    ("01CABIN", "Cabin NVR", "RLN8-410", "not_connected"),
)

# hour, spread in minutes, kind, days in ten it happens, how long it lasts
_COMMUTE = [
    (7.6, 25, "person", 9, 45),
    (7.7, 25, "vehicle", 8, 20),
    (18.4, 55, "vehicle", 9, 20),
    (18.5, 55, "person", 9, 50),
]

CAMERAS = (
    # recorder, channel, name, days of history, continuous footage, playable, routines
    #
    # Drive and Gate are learned and busy, so they carry the marks and the mixed-kind rows.
    ("01VILLA", 0, "Drive", 90, True, True, [*_COMMUTE, (12.5, 90, "person", 3, 30)]),
    ("01VILLA", 1, "Gate", 80, True, True, [*_COMMUTE, (10.0, 120, "vehicle", 4, 12)]),
    # Switched on the day before yesterday: what every installation looks like at first.
    (
        "01VILLA",
        2,
        "Garden",
        2,
        False,
        True,
        [(1.2, 70, "animal", 9, 8), (22.0, 60, "animal", 7, 9)],
    ),
    # Months of days behind it and still too little seen to compare against — the third
    # state, and the one that gets forgotten.
    ("01GARAGE", 0, "Workshop", 60, False, True, [(9.0, 180, "person", 1, 40)]),
    # The rest of the vocabulary: a doorbell, a parcel, a face, and plain motion.
    (
        "01GARAGE",
        1,
        "Front door",
        75,
        True,
        True,
        [
            (8.2, 40, "doorbell", 4, 12),
            (11.0, 150, "package", 3, 18),
            (17.9, 70, "face", 5, 15),
            (13.0, 240, "motion", 8, 25),
            (18.6, 60, "person", 8, 40),
        ],
    ),
    # Nothing here plays: the recorder answers the search and refuses the stream, which is a
    # row the panel has its own wording for.
    ("01GARAGE", 2, "Side alley", 45, True, False, [(2.0, 120, "animal", 6, 10)]),
)

# The odd ones out, planted so every chip colour can be seen with a red icon beside it:
# (recorder, channel, days ago, hour, minute, kind, seconds).
ANOMALIES = (
    ("01VILLA", 0, 2, 2, 41, "person", 192.0),
    ("01VILLA", 0, 5, 14, 5, "animal", 40.0),
    ("01VILLA", 1, 8, 3, 20, "vehicle", 30.0),
    ("01GARAGE", 1, 4, 3, 50, "doorbell", 14.0),
    ("01GARAGE", 1, 9, 4, 15, "package", 22.0),
)

# Signals, per recorder — which is the point.
#
# One Home Assistant often covers more than one property, so whether anybody is home at the
# villa says nothing about the garage. Scoping them to the recorder is the distinctive
# decision in the whole feature, and giving every camera the same two signals made it
# invisible: the sheet looked identical wherever you opened it, and nothing on screen showed
# that the choice was per recorder at all.
#
# So the two recorders watch different things, and a camera's breakdown lists its own.
SIGNALS = {
    "01VILLA": {
        "binary_sensor.someone_home": "Someone home",
        "alarm_control_panel.house": "Alarm",
    },
    "01GARAGE": {
        "cover.garage_door": "Garage door",
        "binary_sensor.workshop_occupied": "Workshop in use",
    },
    # The offline recorder has none, which is its own state worth seeing.
    "01CABIN": {},
}


def signal_labels() -> dict[str, str]:
    """Every signal's friendly name, flattened, as the websocket layer sends them."""
    return {entity_id: name for signals in SIGNALS.values() for entity_id, name in signals.items()}


# A recorder writes 24/7 footage in segments and tags each with whatever fired inside it, so
# one row routinely carries several detections. A row per detection meant the multi-detection
# case — the one the detail sheet pages through — never appeared at all.
SEGMENT_MINUTES = 5

# Long enough that every row can seek somewhere inside it without running off the end.
CLIP_SECONDS = 360


def camera_key(entry_id: str, channel: int) -> str:
    """Return the journal's key for one of the preview cameras."""
    return f"{entry_id}:{channel}"


def _icons() -> dict[str, str]:
    """Return the real Material Design Icons the panel asks for, by name.

    Home Assistant's frontend package ships the whole set as chunked JSON, and it is already
    installed here because the tests need Home Assistant. So the preview gets the actual
    glyphs rather than a stand-in — no network, nothing vendored, and nothing drawn by hand
    and got subtly wrong.
    """
    wanted: set[str] = set()
    for source in FRONTEND.rglob("*.js"):
        wanted.update(re.findall(r"mdi:([a-z0-9-]+)", source.read_text(errors="replace")))
    if not wanted:
        return {}

    try:
        import hass_frontend
    except ImportError:
        return {}

    chunks = Path(hass_frontend.__file__).parent / "static" / "mdi"
    found: dict[str, str] = {}
    for chunk in sorted(chunks.glob("*.json")):
        try:
            data = json.loads(chunk.read_text())
        except (OSError, ValueError):
            continue
        for name in wanted - found.keys():
            if name in data:
                found[name] = data[name]
        if len(found) == len(wanted):
            break
    return found


def _clip() -> Path | None:
    """Return a short MP4 for the player to actually play, making one if needed.

    Without it the player is never reached, and half of what there is to look at — the scrub
    bar, the detection markers, the divider, the zoom, the badge saying how a clip arrived —
    cannot be looked at at all.

    `testsrc` rather than a black frame, because it carries a running counter: seeking to
    2:30 and seeing 2:30 is the difference between the player being on screen and the player
    working. Cached, so only the first run pays for it.
    """
    settings = [
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=12",
        "-t", str(CLIP_SECONDS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-g", "24", "-movflags", "+faststart",
    ]  # fmt: skip
    # Keyed on the settings, so changing them makes a new file instead of quietly serving the
    # one encoded by the old ones.
    stamp = hashlib.sha256(" ".join(settings).encode()).hexdigest()[:8]
    target = Path(tempfile.gettempdir()) / f"reolink-stamina-preview-{stamp}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    if shutil.which("ffmpeg") is None:
        return None

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *settings, str(target)],
        capture_output=True,
        check=False,
    )
    return target if result.returncode == 0 and target.exists() else None


def _context(local: datetime, entry_id: str) -> tuple[tuple[str, str], ...]:
    """Return what the household's signals were doing at that moment.

    Out on most weekdays, in otherwise, with enough variation that the two signals are not
    the same fact twice. That matters more than it looks: the first version derived both from
    one "out at work" boolean, so they agreed perfectly — and the scoring, which adds each
    signal as an independent term, counted that evidence twice. Two large negatives for the
    commonest state in the data then swamped everything else, and a vehicle at twenty past
    three in the morning came out *more common than chance*.

    The independence assumption is false in any real household too, which is the known cost
    of adding terms rather than conditioning on them. Signals that are near copies of each
    other will overstate themselves, and that is worth knowing before pointing this at four
    entities that all mean "somebody is in".
    """
    # Deterministic per day, so a reload invents the same household.
    rng = random.Random(local.toordinal())
    workday = local.weekday() < 5 and rng.random() > 0.15
    out = workday and 9 <= local.hour < 17
    # Sometimes out in the evening too, and the alarm is not always set when they are.
    if not out and rng.random() > 0.85 and 19 <= local.hour < 23:
        out = True
    armed = out and rng.random() > 0.25

    if entry_id == "01VILLA":
        return (
            ("binary_sensor.someone_home", "off" if out else "on"),
            ("alarm_control_panel.house", "armed_away" if armed else "disarmed"),
        )
    if entry_id == "01GARAGE":
        # A door that is open while somebody is working, and shut the rest of the time.
        working = not out and 8 <= local.hour < 19 and rng.random() > 0.5
        return (
            ("cover.garage_door", "open" if working else "closed"),
            ("binary_sensor.workshop_occupied", "on" if working else "off"),
        )
    return ()


def _detections(seed: int, days: int | None) -> list[tuple[str, int, float, str, float]]:
    """Invent the household: (recorder, channel, when, kind, seconds)."""
    rng = random.Random(seed)
    now = datetime.now().astimezone()
    # Local midnight, not UTC's: a household's routine is in local time, and building the day
    # in UTC put the small-hours visit at 04:41 in a summer European timezone.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found: list[tuple[str, int, float, str, float]] = []

    for entry_id, channel, _name, history, _continuous, _playable, routines in CAMERAS:
        for day in range(days if days is not None else history, -1, -1):
            base = midnight - timedelta(days=day)
            weekend = base.weekday() >= 5
            for hour, spread, kind, odds, seconds in routines:
                if rng.randint(1, 10) > odds:
                    continue
                # People get up later at the weekend, and so does everything that follows.
                shift = 1.4 if weekend and hour < 12 else 0.0
                at = base + timedelta(hours=hour + shift, minutes=rng.gauss(0, spread / 3.0))
                if at <= now:
                    found.append(
                        (
                            entry_id,
                            channel,
                            at.timestamp(),
                            kind,
                            max(3.0, rng.gauss(seconds, seconds / 3)),
                        )
                    )

    # An arrival is not one detection: a car pulls in and somebody gets out of it, so the two
    # land in the same segment and the row reads "Person (2)". Without this the preview made
    # one detection per segment and the sheet's paging was never reachable.
    for entry_id, channel, at, kind, _seconds in list(found):
        if kind != "vehicle" or rng.random() > 0.65:
            continue
        found.append(
            (entry_id, channel, at + rng.uniform(25, 110), "person", max(4.0, rng.gauss(30, 10)))
        )

    # One odd one out per kind, so every chip colour can be seen with a red icon beside it.
    for entry_id, channel, ago, hour, minute, kind, seconds in ANOMALIES:
        history = next((row[3] for row in CAMERAS if row[0] == entry_id and row[1] == channel), 0)
        if (days if days is not None else history) < ago:
            continue
        at = midnight - timedelta(days=ago) + timedelta(hours=hour, minutes=minute)
        if at <= now:
            found.append((entry_id, channel, at.timestamp(), kind, seconds))

    found.sort(key=lambda item: item[2])
    return found


def _model(detections):
    """Run the invented detections through the real engine."""
    events = []
    for entry_id, channel, at, kind, seconds in detections:
        local = datetime.fromtimestamp(at).astimezone()
        events.append(
            Event(
                camera=camera_key(entry_id, channel),
                kind=kind,
                started_at=at,
                ended_at=at + seconds,
                duration=round(seconds, 1),
                minute_of_day=local.hour * 60 + local.minute,
                # Skipped rather than faked: sunset needs a location, and inventing one would
                # put a number in the breakdown that means nothing.
                solar_offset=None,
                is_weekend=local.weekday() >= 5,
                context=_context(local, entry_id),
            )
        )
    model = build(events, now=datetime.now(UTC).timestamp())
    calibrate(model, events)
    return events, model


def _rows(detections) -> dict[str, list[dict]]:
    """Group detections into recording segments, the way a recorder writes them."""
    facts = {
        (entry_id, channel): (name, continuous, playable)
        for entry_id, channel, name, _days, continuous, playable, _routines in CAMERAS
    }
    devices = {entry_id: name for entry_id, name, _model, _status in RECORDERS}
    span = SEGMENT_MINUTES * 60
    segments: dict[tuple[str, int, int], list[tuple[float, str, float]]] = {}

    for entry_id, channel, at, kind, seconds in detections:
        segments.setdefault((entry_id, channel, int(at // span)), []).append((at, kind, seconds))

    buckets: dict[str, list[dict]] = {}
    for (entry_id, channel, index), inside in sorted(segments.items()):
        name, continuous, playable = facts[(entry_id, channel)]
        start = datetime.fromtimestamp(index * span, UTC)
        # A camera recording on events writes a clip the length of the event; one recording
        # 24/7 writes a fixed segment whatever happened inside it.
        length = span if continuous else max(20.0, max(s for _, _, s in inside) + 16)
        end = start + timedelta(seconds=length)
        date = datetime.fromtimestamp(index * span).astimezone().date().isoformat()
        counts: dict[str, int] = {}
        for _, kind, _ in inside:
            counts[kind] = counts.get(kind, 0) + 1

        buckets.setdefault(f"{entry_id}|{channel}|{date}", []).append(
            {
                "id": f"{entry_id}:{channel}:{start.strftime('%Y%m%d%H%M%S')}",
                "entry_id": entry_id,
                "device": devices[entry_id],
                "channel": channel,
                "camera": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration": round(length),
                "triggers": sorted(counts),
                "kinds": sorted(counts),
                "counts": counts,
                "size": int(length * 180_000),
                "size_is_exact": True,
                "streams": ["sub"],
                "files": ["sub"],
                "playable": playable,
                "continuous": continuous,
                "alternate_streams": ["main"],
                "pre_roll": 5,
            }
        )

    for events in buckets.values():
        events.sort(key=lambda event: event["start"], reverse=True)
    return buckets


def build_fixtures(seed: int = 1, days: int | None = None) -> dict:
    """Everything the harness needs to answer the panel's commands."""
    detections = _detections(seed, days)
    events, model = _model(detections)
    rows = _rows(detections)
    clip = _clip()
    if clip is None:
        for day in rows.values():
            for row in day:
                row["playable"] = False

    names = {
        camera_key(entry_id, channel): name for entry_id, channel, name, _d, _c, _p, _r in CAMERAS
    }
    relevance: dict[str, dict] = {}
    for key in names:
        profile = model.per_camera.get(key)
        relevance[key] = {
            "state": (
                "collecting"
                if profile is None or profile.events == 0 or profile.days < SCORE_MIN_DAYS
                else "active"
                if ready(profile)
                else "too_few_events"
            ),
            "coverage": {
                "days": round(profile.days, 1) if profile else 0.0,
                "events": profile.events if profile else 0,
            },
            "needs": {"days": SCORE_MIN_DAYS, "events": SCORE_MIN_EVENTS},
            "events": [],
        }

    previous = None
    for event in events:
        result = score(event, model, previous=previous, names=names, labels=signal_labels())
        previous = event
        relevance[event.camera]["events"].append(
            {
                "at": datetime.fromtimestamp(event.started_at, UTC).isoformat(),
                "kind": event.kind,
                "duration": event.duration,
                "score": round(result.total, 2),
                "threshold": None if result.threshold is None else round(result.threshold, 2),
                "unusual": result.unusual,
                "reason": result.reason,
                "terms": [
                    {
                        "name": term.name,
                        "subject": term.subject,
                        "label": term.label,
                        "contribution": round(term.contribution, 2),
                        "seen": term.seen,
                    }
                    for term in result.terms
                ],
            }
        )

    marked = sum(1 for camera in relevance.values() for e in camera["events"] if e["unusual"])
    return {
        "icons": _icons(),
        "playable": clip is not None,
        "devices": [
            {
                "entry_id": entry_id,
                "name": name,
                "status": status,
                "model": model_name,
                "sw_version": "v3.6.5",
                "connected": status == "ok",
                "has_storage": status == "ok",
                "reports_triggers": True,
                "kind": "nvr",
                "cameras": [
                    {
                        "channel": channel,
                        "name": camera,
                        "ai_types": sorted({row[2] for row in routines}),
                        "streams": ["main", "sub"],
                        "can_playback": True,
                        "pre_record": {"supported": False, "enabled": False, "seconds": None},
                    }
                    for cam_entry, channel, camera, _d, _c, _p, routines in CAMERAS
                    if cam_entry == entry_id
                ],
            }
            for entry_id, name, model_name, status in RECORDERS
        ],
        "buckets": rows,
        "relevance": relevance,
        "summary": {
            "detections": len(events),
            "marked": marked,
            "recorders": len(RECORDERS),
            "cameras": len(CAMERAS),
        },
    }


HARNESS = """<!doctype html>
<meta charset="utf-8">
<title>Reolink Stamina — preview</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  /*
   * Home Assistant's own theme variables.
   *
   * The panel never asks `hass` what the theme is — every one of its tokens is
   * `var(--primary-background-color, <light default>)` and friends, inherited from the
   * document, which is how it follows a custom theme without knowing anything about themes.
   * So a harness that defines none of them gets the light fallbacks for ever, whatever the
   * machine prefers. These are Home Assistant's default light and dark values.
   *
   * Custom properties inherit across shadow boundaries, so declaring them here reaches every
   * shadow root the panel builds.
   */
  :root {
    color-scheme: light dark;
    --primary-color: #03a9f4;
    --primary-background-color: #fafafa;
    --secondary-background-color: #e5e5e5;
    --card-background-color: #ffffff;
    --ha-card-background: #ffffff;
    --primary-text-color: #212121;
    --secondary-text-color: #727272;
    --text-primary-color: #ffffff;
    --divider-color: rgba(0, 0, 0, 0.12);
    --error-color: #db4437;
    --warning-color: #ffa600;
    --success-color: #43a047;
    --ha-font-family-body: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  /* `:not([data-theme="light"])` so ?theme=light still wins on a dark machine. */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --primary-background-color: #111111;
      --secondary-background-color: #202020;
      --card-background-color: #1c1c1c;
      --ha-card-background: #1c1c1c;
      --primary-text-color: #e1e1e1;
      --secondary-text-color: #9b9b9b;
      --divider-color: rgba(225, 225, 225, 0.12);
    }
  }
  /* And again for the explicit override, so it wins in the other direction too. */
  :root[data-theme="dark"] {
    --primary-background-color: #111111;
    --secondary-background-color: #202020;
    --card-background-color: #1c1c1c;
    --ha-card-background: #1c1c1c;
    --primary-text-color: #e1e1e1;
    --secondary-text-color: #9b9b9b;
    --divider-color: rgba(225, 225, 225, 0.12);
  }

  html, body {
    margin: 0;
    height: 100%;
    overflow: hidden;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--ha-font-family-body);
  }
  /*
   * The banner floats rather than sitting above the panel.
   *
   * The panel's own host rule is 100dvh, because in Home Assistant the panel *is* the
   * viewport. Anything in normal flow above it therefore makes the page taller than the
   * window, which pushes the list's scroller off the bottom — the list stops scrolling and
   * the page scrolls instead. Floating it keeps the preview's geometry identical to the
   * real thing, which is the whole point of previewing.
   */
  #banner {
    position: fixed; z-index: 9999; left: 10px; bottom: 10px;
    font: 11px/1.5 ui-monospace, Menlo, monospace; letter-spacing: .02em;
    padding: 5px 11px; border-radius: 999px;
    background: rgba(20, 20, 22, 0.86); color: #fff;
    backdrop-filter: blur(6px);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
  }
  #banner b { font-weight: 700; color: #ff8a8f; }
  #banner a { color: #fff; opacity: .8; }
  #banner a:hover { opacity: 1; }
</style>
<div id="banner"><b>PREVIEW</b> — invented data. The picture is a test pattern, not footage.
  <span id="stats"></span> · <a href="?theme=light">light</a> <a href="?theme=dark">dark</a>
  <a href="?">system</a></div>
<reolink-stamina-panel id="panel"></reolink-stamina-panel>

<script type="module">
  // The machine's preference by default; ?theme=light|dark to look at the other one without
  // changing a system setting, which is the whole reason to want it during UX work.
  const asked = new URLSearchParams(location.search).get("theme");
  if (asked === "light" || asked === "dark") document.documentElement.dataset.theme = asked;

  const dark = matchMedia("(prefers-color-scheme: dark)");
  const isDark = () => (asked ? asked === "dark" : dark.matches);

  const fixtures = await (await fetch("./api/fixtures.json")).json();

  // Home Assistant's own elements are not here, and the panel needs exactly two of them.
  //
  // The glyphs are the real Material Design Icons, lifted out of the installed Home
  // Assistant frontend — so what you see is what the panel draws, rather than a stand-in
  // that happens to occupy the same box.
  //
  // A shadow root, and sizing from CSS rather than from attributes: the host is sized by
  // the panel's own `.icon` rules, and an <svg> at 100% of it needs no measuring. Reading
  // `--mdc-icon-size` and writing width attributes is what made the first attempt at this
  // draw nothing at all.
  if (!customElements.get("ha-icon")) {
    customElements.define("ha-icon", class extends HTMLElement {
      static get observedAttributes() { return ["icon"]; }
      connectedCallback() { this._paint(); }
      attributeChangedCallback() { this._paint(); }
      _paint() {
        if (!this.shadowRoot) this.attachShadow({ mode: "open" });
        const name = (this.getAttribute("icon") || "").replace(/^mdi:/, "");
        const path = fixtures.icons[name];
        this.shadowRoot.innerHTML =
          `<style>:host{display:inline-flex;align-items:center;justify-content:center}` +
          `svg{width:100%;height:100%;display:block}</style>` +
          `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">` +
          (path
            ? `<path d="${path}"/>`
            : `<circle cx="12" cy="12" r="7" opacity="0.5"/>`) +
          `</svg>`;
      }
    });
  }
  if (!customElements.get("ha-menu-button")) {
    customElements.define("ha-menu-button", class extends HTMLElement {});
  }
  document.getElementById("stats").textContent =
    `${fixtures.summary.detections} detections over ${fixtures.summary.days} days · ` +
    `${fixtures.summary.marked} marked unusual`;

  const key = (entryId, channel) => `${entryId}:${channel}`;
  const CLIP = "/preview/clip.mp4";

  function bucketsFor(targets, startDate, endDate) {
    const out = [];
    for (const target of targets) {
      const last = new Date(endDate);
      for (let day = new Date(startDate); day <= last; day.setDate(day.getDate() + 1)) {
        const date = day.toISOString().slice(0, 10);
        const id = `${target.entry_id}|${target.channel}|${date}`;
        out.push({
          key: id,
          entry_id: target.entry_id,
          channel: target.channel,
          date,
          events: fixtures.buckets[id] || [],
          loaded: true,
          age: 3.2,
          error: null,
          updating: false,
          unlabelled_skipped: 0,
        });
      }
    }
    return out;
  }

  const hass = {
    language: "en",
    // The panel does not read this — it follows the CSS variables above, exactly as it does
    // in Home Assistant — but anything added later that does should find the truth here.
    themes: { darkMode: isDark() },
    user: { is_admin: true },

    async callWS(msg) {
      const type = msg.type.split("/")[1];
      if (type === "devices") {
        return {
          devices: fixtures.devices,
          options: {
            browse_stream: "sub", split_minutes: 5, hide_timer: true, pre_roll: 5,
            require_admin: true, include_unlabelled: false, event_lead: 30,
            clip_lead: 15, clip_tail: 15, verify_tls: false,
            // On, because the stand-in recording is an MP4 rather than the FLV a real
            // recorder sends: the passthrough rung refuses here, the ladder falls to the
            // next one exactly as it would in the field, and that rung is served.
            beta_restream: true, beta_all_devices: false, beta_relevance: true,
          },
          search_window_days: 30,
        };
      }
      if (type === "relevance") {
        const known = fixtures.relevance[key(msg.entry_id, msg.channel)];
        if (!known) return { enabled: false };
        const from = Date.parse(msg.start), to = Date.parse(msg.end);
        return {
          enabled: true,
          state: known.state,
          coverage: known.coverage,
          events: known.events.filter((item) => {
            const at = Date.parse(item.at);
            return at >= from && at < to;
          }),
        };
      }
      if (type === "detections") {
        // The moments inside the row, which is what puts the markers on the scrub bar.
        const known = fixtures.relevance[key(msg.entry_id, msg.channel)];
        const from = Date.parse(msg.start), to = Date.parse(msg.end);
        const detections = !known ? [] : known.events
          .filter((item) => { const at = Date.parse(item.at); return at >= from && at < to; })
          .map((item) => ({
            at: item.at,
            kind: item.kind,
            offset: (Date.parse(item.at) - from) / 1000,
            until: new Date(Date.parse(item.at) + (item.duration || 5) * 1000).toISOString(),
            end_offset: (Date.parse(item.at) - from) / 1000 + (item.duration || 5),
          }));
        return { detections, lead: 30, clip_lead: 15, clip_tail: 15 };
      }
      if (type === "playback_failure") return {};
      if (msg.type === "auth/sign_path") return { path: msg.path };
      if (type === "clip_url") return { path: CLIP };
      if (type === "stream_url") {
        if (!fixtures.playable) throw new Error("ffmpeg was not available to make a clip.");
        if (msg.route === "passthrough") {
          // The recorder's own FLV, which there is none of. Refusing is what sends the
          // player down its ladder, and exercising that is worth more than shortcutting it.
          throw new Error("No recorder: the preview serves the converted route instead.");
        }
        return {
          seek: msg.seek || 0,
          seekable: true,
          route: msg.route,
          mime: "video/mp4",
          path: CLIP,
          sign: false,
        };
      }
      throw new Error("There is no recorder behind this preview.");
    },

    connection: {
      async subscribeMessage(callback, msg) {
        const type = msg.type.split("/")[1];
        if (type === "events") {
          // Delivered on a tick, exactly as the real one arrives after the round trip.
          setTimeout(() => callback({
            type: "snapshot",
            primary_stream: "sub",
            secondary_stream: "main",
            start_date: msg.start_date,
            end_date: msg.end_date,
            truncated: false,
            buckets: bucketsFor(msg.targets, msg.start_date, msg.end_date),
          }), 60);
        } else if (type === "calendar") {
          setTimeout(() => callback({
            type: "snapshot",
            year: msg.year,
            month: msg.month,
            cameras: msg.targets.map((target) => {
              const days = new Set();
              for (const id of Object.keys(fixtures.buckets)) {
                const [entry, channel, date] = id.split("|");
                const [year, month, day] = date.split("-").map(Number);
                if (entry === target.entry_id && Number(channel) === target.channel &&
                    year === msg.year && month === msg.month) days.add(day);
              }
              return { entry_id: target.entry_id, channel: target.channel, days: [...days] };
            }),
          }), 60);
        }
        return () => {};
      },
    },
  };

  await import("./frontend/reolink-stamina-panel.js");
  const panel = document.getElementById("panel");
  panel.panel = { config: { options: {}, version: "preview" } };
  panel.narrow = false;
  panel.hass = hass;
</script>
"""


class Handler(SimpleHTTPRequestHandler):
    """Serves the harness, the real frontend, and the invented data."""

    def __init__(self, *args, build, clip: Path | None, **kwargs) -> None:
        """Keep the recipe rather than the data, and where the stand-in clip is."""
        self._build = build
        self._clip = clip
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def do_GET(self) -> None:
        """Answer the two generated paths, and serve the panel's own files for the rest."""
        if self.path in ("/", "/index.html"):
            return self._send(HARNESS.encode(), "text/html; charset=utf-8")
        if self.path == "/api/fixtures.json":
            # Rebuilt on every request, not held from startup. It costs under a tenth of a
            # second and it means editing this script behaves like editing the panel does —
            # reload the page and the change is there. Held once, it silently served the data
            # it was started with, and a fix to the data looked like a fix that did nothing.
            return self._send(json.dumps(self._build()).encode(), "application/json")
        if self.path.split("?")[0] == "/preview/clip.mp4":
            return self._send_clip()
        if self.path.startswith("/frontend/"):
            self.path = self.path[len("/frontend") :]
            return super().do_GET()
        return super().do_GET()

    def _send_clip(self) -> None:
        """Serve the stand-in recording, honouring Range.

        `SimpleHTTPRequestHandler` answers every request with the whole file, and a browser
        given a 200 for a video will play it from the start and refuse to seek. Seeking is
        most of what the scrub bar is for, so the one header that makes it work is worth the
        twenty lines.
        """
        clip = self._clip
        if not clip or not clip.exists():
            self.send_error(404, "No clip: ffmpeg was not available when this started")
            return

        data = clip.read_bytes()
        total = len(data)
        start, end = 0, total - 1
        requested = self.headers.get("Range", "")
        partial_request = requested.startswith("bytes=")
        if partial_request:
            first, _, last = requested[len("bytes=") :].partition("-")
            start = int(first) if first else 0
            end = int(last) if last else total - 1
            start = max(0, min(start, total - 1))
            end = max(start, min(end, total - 1))

        body = data[start : end + 1]
        self.send_response(206 if partial_request else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if partial_request:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        # A browser that seeks away mid-response closes the socket. Expected, not news.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _send(self, body: bytes, content_type: str) -> None:
        """Write one generated response."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here may be cached: the whole point is seeing an edit immediately.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        """Stop the browser caching the panel's modules between edits."""
        if self.path.startswith("/frontend/") or self.path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        """Quieter than the default, which logs every module on every reload."""
        if "404" in (fmt % args):
            sys.stderr.write(f"  missing: {args[0]}\n")


def _worth_looking_at(fixtures: dict) -> list[dict]:
    """Return the marked rows, so the banner can say where to look.

    Grouped by row rather than by detection — a segment can hold two marked detections of
    different kinds, and listing it twice reads as two rows that disagree — and picked so
    every kind that was marked appears at least once. A list that happens to be four people
    proves the person chip reddens and nothing else.
    """
    rows = [row for day in fixtures["buckets"].values() for row in day]
    by_row: dict[str, dict] = {}
    for key, known in fixtures["relevance"].items():
        for event in known["events"]:
            if not event["unusual"]:
                continue
            for row in rows:
                if f"{row['entry_id']}:{row['channel']}" != key:
                    continue
                if row["start"] <= event["at"] <= row["end"]:
                    found = by_row.setdefault(
                        row["id"],
                        {
                            "when": f"{row['start'][:10]} {row['start'][11:16]}",
                            "camera": row["camera"],
                            "device": row["device"],
                            "kinds": row["kinds"],
                            "red": set(),
                        },
                    )
                    found["red"].add(event["kind"])
                    break

    ordered = sorted(
        by_row.values(), key=lambda item: (len(item["kinds"]) > 1, item["when"]), reverse=True
    )
    picked: list[dict] = []
    for kind in sorted({kind for item in ordered for kind in item["red"]}):
        first = next((i for i in ordered if kind in i["red"] and i not in picked), None)
        if first is not None:
            picked.append(first)
    for item in ordered:
        if len(picked) >= 8:
            break
        if item not in picked:
            picked.append(item)
    return picked


def main() -> int:
    """Invent a household, then serve the panel against it."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--seed", type=int, default=1, help="change for a different household")
    parser.add_argument(
        "--days", type=int, help="give every camera the same history, overriding the defaults"
    )
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    if not FRONTEND.is_dir():
        parser.error(f"no frontend at {FRONTEND}")

    def build() -> dict:
        """Invent it afresh. Deterministic, so a reload is the same household."""
        return build_fixtures(args.seed, args.days)

    fixtures = build()
    summary = fixtures["summary"]
    url = f"http://127.0.0.1:{args.port}/"

    print(f"\n  Reolink Stamina preview — {url}")
    print(
        f"  {summary['recorders']} recorders · {summary['cameras']} cameras · "
        f"{summary['detections']} detections · {summary['marked']} marked unusual"
    )
    for entry_id, name, _model, status in RECORDERS:
        note = "" if status == "ok" else f"   ({status.replace('_', ' ')})"
        signals = SIGNALS.get(entry_id) or {}
        watching = ", ".join(signals.values()) if signals else "no signals"
        print(f"\n    {name}{note}   ·   watching: {watching}")
        for cam_entry, channel, camera, _d, continuous, playable, _r in CAMERAS:
            if cam_entry != entry_id:
                continue
            known = fixtures["relevance"][camera_key(entry_id, channel)]
            kind = "24/7" if continuous else "on events"
            plays = "" if playable else "  · nothing plays"
            print(
                f"      {camera:<12} {known['state']:<15} "
                f"{known['coverage']['days']:>5.1f}d {known['coverage']['events']:>4} detections"
                f"  · {kind}{plays}"
            )

    marked = _worth_looking_at(fixtures)
    if marked:
        print("\n  Rows worth opening — the red icon should be on the kind named:")
        for item in marked:
            note = "   <-- several chips" if len(item["kinds"]) > len(item["red"]) else ""
            print(
                f"    {item['when']}  {item['camera']:<12} {item['kinds']!s:<28} "
                f"red = {sorted(item['red'])}{note}"
            )

    print("\n  Panel modules and sample data are both rebuilt on reload.", flush=True)
    print("  Ctrl-C to stop.\n", flush=True)

    if not args.no_open:
        webbrowser.open(url)

    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", args.port),
            partial(Handler, build=build, clip=_clip()),
        )
    except OSError as err:
        # Almost always something already listening. A traceback for that is unkind.
        print(f"\n  Cannot listen on port {args.port}: {err}")
        print(f"  Something else is using it. Try: make preview PORT={args.port + 1}\n")
        return 1

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
