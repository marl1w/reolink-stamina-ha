#!/usr/bin/env python3
"""Serve the real panel on this machine, against invented data.

The panel is a graph of ES modules that talks to Home Assistant through exactly two things:
`hass.callWS` and `hass.connection.subscribeMessage`. Give it stand-ins for those and it runs
anywhere — so this serves the actual files out of `custom_components/reolink_stamina/frontend`
and answers their websocket commands from a made-up household.

    scripts/preview.py                 # http://127.0.0.1:8123
    scripts/preview.py --port 9000
    scripts/preview.py --days 45 --seed 7

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
from custom_components.reolink_stamina.relevance.score import calibrate, ready, score  # noqa: E402

ENTRY = "01PREVIEWNVR"

# Long enough that every row can seek somewhere inside it without running off the end.
CLIP_SECONDS = 360

# One recorder, three cameras, each with its own habits — because the whole point of the
# feature is that "normal" is a property of a camera, not of the household.
CAMERAS = [
    {"channel": 0, "name": "Drive", "kinds": ["person", "vehicle"]},
    {"channel": 1, "name": "Gate", "kinds": ["vehicle", "person"]},
    {"channel": 2, "name": "Garden", "kinds": ["animal", "person"]},
]

# hour, minutes-of-spread, kind, how many days in ten it happens, how long it lasts
ROUTINES = {
    0: [
        (7.6, 25, "person", 9, 45),
        (7.7, 25, "vehicle", 8, 20),
        (18.4, 55, "vehicle", 9, 20),
        (18.5, 55, "person", 9, 50),
        (12.5, 90, "person", 3, 30),
    ],
    1: [
        (7.7, 25, "vehicle", 8, 15),
        (18.4, 55, "vehicle", 9, 15),
        (10.0, 120, "vehicle", 4, 12),
        (14.0, 180, "person", 2, 25),
    ],
    2: [
        (1.2, 70, "animal", 9, 8),
        (2.6, 80, "animal", 7, 7),
        (23.0, 60, "animal", 6, 9),
        (16.0, 120, "person", 4, 120),
    ],
}


def camera_key(channel: int) -> str:
    """Return the journal's key for one of the preview cameras."""
    return f"{ENTRY}:{channel}"


def _icons() -> dict[str, str]:
    """Return the real Material Design Icons the panel asks for, by name.

    Home Assistant's frontend package ships the whole set as chunked JSON, and it is already
    installed here because the tests need Home Assistant. So the preview gets the actual
    glyphs rather than a stand-in — no network, nothing vendored, and no drawing anything by
    hand and getting it subtly wrong.

    Only the icons the panel actually names are kept: the full set is five megabytes, and the
    panel uses sixty of them.
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

    Without this the player is never reached, and half of what there is to look at — the
    scrub bar, the detection markers, the split divider, the zoom, the badge that says how a
    clip arrived — cannot be looked at at all.

    `testsrc` rather than a black frame, because it carries a running counter: seeking to
    2:30 and seeing 2:30 is the difference between "the player is on screen" and "the player
    works". Cached in the temp directory, so only the first run pays for it.
    """
    settings = [
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=12",
        "-t",
        str(CLIP_SECONDS),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "24",
        "-movflags",
        "+faststart",
    ]
    # Keyed on the settings, so changing them makes a new file instead of quietly serving
    # the one encoded by the old ones.
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
    if result.returncode != 0 or not target.exists():
        return None
    return target


def _detections(days: int, seed: int) -> list[tuple[int, float, str, float]]:
    """Invent a household's worth of detections: (channel, when, kind, seconds)."""
    rng = random.Random(seed)
    now = datetime.now().astimezone()
    # Local midnight, not UTC's. A household's routine is in local time, and building the
    # day in UTC put the small-hours visit at 04:41 in a summer European timezone.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found: list[tuple[int, float, str, float]] = []

    for day in range(days, -1, -1):
        base = midnight - timedelta(days=day)
        weekend = base.weekday() >= 5
        for channel, routines in ROUTINES.items():
            for hour, spread, kind, odds, seconds in routines:
                if rng.randint(1, 10) > odds:
                    continue
                # People get up later at the weekend, and so does everything that follows.
                shift = 1.4 if weekend and hour < 12 else 0.0
                at = base + timedelta(hours=hour + shift, minutes=rng.gauss(0, spread / 3.0))
                if at > now:
                    continue
                found.append(
                    (channel, at.timestamp(), kind, max(3.0, rng.gauss(seconds, seconds / 3)))
                )

    # And the thing the feature exists for: somebody on the drive in the small hours, two
    # nights ago, staying a while and arriving without passing the gate first.
    prowler = (midnight - timedelta(days=2) + timedelta(hours=2, minutes=41)).timestamp()
    found.append((0, prowler, "person", 192.0))

    found.sort(key=lambda item: item[1])
    return found


def _model(detections):
    """Run the invented detections through the real engine."""
    events = []
    for channel, at, kind, seconds in detections:
        local = datetime.fromtimestamp(at).astimezone()
        events.append(
            Event(
                camera=camera_key(channel),
                kind=kind,
                started_at=at,
                ended_at=at + seconds,
                duration=round(seconds, 1),
                minute_of_day=local.hour * 60 + local.minute,
                # Skipped rather than faked: sunset needs a location, and inventing one
                # would put a number in the breakdown that means nothing.
                solar_offset=None,
                is_weekend=local.weekday() >= 5,
            )
        )
    model = build(events, now=datetime.now(UTC).timestamp())
    calibrate(model, events)
    return events, model


def _rows(detections, *, playable: bool) -> dict[str, list[dict]]:
    """Turn detections into the event rows the panel lists, keyed by `entry|channel|date`."""
    names = {camera["channel"]: camera["name"] for camera in CAMERAS}
    buckets: dict[str, list[dict]] = {}

    for channel, at, kind, seconds in detections:
        start = datetime.fromtimestamp(at, UTC) - timedelta(seconds=8)
        end = start + timedelta(seconds=seconds + 16)
        date = datetime.fromtimestamp(at).astimezone().date().isoformat()
        key = f"{ENTRY}|{channel}|{date}"
        stamp = start.strftime("%Y%m%d%H%M%S")
        buckets.setdefault(key, []).append(
            {
                "id": f"{ENTRY}:{channel}:{stamp}",
                "entry_id": ENTRY,
                "device": "Preview NVR",
                "channel": channel,
                "camera": names[channel],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration": round((end - start).total_seconds()),
                "triggers": [kind],
                "kinds": [kind],
                "counts": {kind: 1},
                "size": int(seconds * 180_000),
                "size_is_exact": True,
                "streams": ["sub"],
                "files": ["sub"],
                # True once ffmpeg has made the stand-in clip; without one the player has
                # nothing to open and the row says so, exactly as an unplayable row would.
                "playable": playable,
                "continuous": False,
                "alternate_streams": ["main"],
                "pre_roll": 5,
            }
        )

    for events in buckets.values():
        events.sort(key=lambda event: event["start"], reverse=True)
    return buckets


def build_fixtures(days: int, seed: int) -> dict:
    """Everything the harness needs to answer the panel's commands."""
    detections = _detections(days, seed)
    events, model = _model(detections)
    clip = _clip()
    rows = _rows(detections, playable=clip is not None)

    names = {camera_key(camera["channel"]): camera["name"] for camera in CAMERAS}
    relevance: dict[str, dict] = {}
    for camera in CAMERAS:
        key = camera_key(camera["channel"])
        profile = model.per_camera.get(key)
        relevance[key] = {
            "state": (
                "collecting"
                if profile is None or profile.events == 0
                else "active"
                if ready(profile)
                else "collecting"
                if profile.days < 14
                else "too_few_events"
            ),
            "coverage": {
                "days": round(profile.days, 1) if profile else 0.0,
                "events": profile.events if profile else 0,
            },
            "events": [],
        }

    previous = None
    for event in events:
        result = score(event, model, previous=previous, names=names)
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
                        "label": term.label,
                        "contribution": round(term.contribution, 2),
                        "seen": term.seen,
                    }
                    for term in result.terms
                ],
            }
        )

    marked = sum(1 for camera in relevance.values() for item in camera["events"] if item["unusual"])
    icons = _icons()
    return {
        "icons": icons,
        "playable": clip is not None,
        "devices": [
            {
                "entry_id": ENTRY,
                "name": "Preview NVR",
                "status": "ok",
                "model": "RLN8-410",
                "sw_version": "v3.6.5",
                "connected": True,
                "has_storage": True,
                "reports_triggers": True,
                "kind": "nvr",
                "cameras": [
                    {
                        "channel": camera["channel"],
                        "name": camera["name"],
                        "ai_types": camera["kinds"],
                        "streams": ["main", "sub"],
                        "can_playback": True,
                        "pre_record": {"supported": False, "enabled": False, "seconds": None},
                    }
                    for camera in CAMERAS
                ],
            }
        ],
        "buckets": rows,
        "relevance": relevance,
        "summary": {
            "detections": len(events),
            "marked": marked,
            "days": days,
            "icons": len(icons),
            "clip": str(clip) if clip else None,
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

    def __init__(self, *args, fixtures: dict, **kwargs) -> None:
        """Keep the generated data to answer with."""
        self._fixtures = fixtures
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def do_GET(self) -> None:
        """Answer the two generated paths, and serve the panel's own files for the rest."""
        if self.path in ("/", "/index.html"):
            return self._send(HARNESS.encode(), "text/html; charset=utf-8")
        if self.path == "/api/fixtures.json":
            return self._send(json.dumps(self._fixtures).encode(), "application/json")
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
        clip = self._fixtures["summary"].get("clip")
        if not clip or not Path(clip).exists():
            self.send_error(404, "No clip: ffmpeg was not available when this started")
            return

        data = Path(clip).read_bytes()
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


def main() -> int:
    """Generate a household, then serve the panel against it."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--days", type=int, default=60, help="how much history to invent")
    parser.add_argument("--seed", type=int, default=1, help="change for a different household")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    if not FRONTEND.is_dir():
        parser.error(f"no frontend at {FRONTEND}")

    fixtures = build_fixtures(args.days, args.seed)
    summary = fixtures["summary"]
    url = f"http://127.0.0.1:{args.port}/"

    print(f"\n  Reolink Stamina preview — {url}")
    print(f"  {summary['detections']} detections over {summary['days']} days, ", end="")
    print(f"{summary['marked']} marked unusual")
    for camera in CAMERAS:
        known = fixtures["relevance"][camera_key(camera["channel"])]
        print(
            f"    {camera['name']:<8} {known['state']:<14} "
            f"{known['coverage']['days']:>5.1f} days  {known['coverage']['events']:>4} detections"
        )
    # Flushed, because a dev tool whose banner only appears when you stop it is no use.
    print("\n  Serving the real panel modules — edit one and reload.", flush=True)
    print("  Ctrl-C to stop.\n", flush=True)

    if not args.no_open:
        webbrowser.open(url)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, fixtures=fixtures))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
