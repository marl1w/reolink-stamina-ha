/**
 * Clip playback.
 *
 * Three behaviours are deliberate and load-bearing:
 *
 * 1. On a camera recording on events, playback starts at the beginning of the recording
 *    and covers all of it. Reolink writes the pre-record buffer into the start of the
 *    file, so this is what guarantees the pre-motion footage is actually seen — nothing
 *    is trimmed or skipped there.
 * 2. On a camera recording 24/7 there is no such buffer to protect: the event sits
 *    somewhere inside a fixed segment. Those clips are cut to the detections in them,
 *    with the whole segment always one button away.
 * 3. The trigger marker on the scrub bar is drawn solid only when the camera reports
 *    its own pre-record time. Otherwise it is dashed and labelled as an estimate,
 *    because wired cameras on an NVR generally do not expose their pre-alarm setting.
 *
 * Custom controls rather than the browser's, because a native control bar cannot be
 * overlaid with the trigger marker.
 */

import { adoptStyles, el, icon } from "../dom.js";
import { SHARED } from "../theme.js";
import { downloadClip } from "../clip.js";
import { formatClock, formatSize, formatTime, sortTriggers, streamLabel, triggerMeta } from "../format.js";

/**
 * A ceiling on a clip download, because it is assembled in memory.
 *
 * The low-resolution stream runs about 1 Mbit/s, so this is roughly forty minutes of it —
 * far more than any clip — while still refusing to fill a phone's memory with a
 * high-resolution recording someone asked for by accident.
 */
const MAX_CLIP_BYTES = 300 * 1024 * 1024;

/** Hand a finished clip to the browser's downloads. */
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  // Give the download a moment to start before the bytes are released.
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

/**
 * Whether this browser can decode H.265/HEVC.
 *
 * Reolink NVRs commonly encode the main stream as HEVC and the sub stream as H.264.
 * Safari plays HEVC; Chrome and Firefox generally do not. Getting this wrong looks like
 * "the NVR is broken" when the bytes arrived perfectly well.
 */
const HEVC_SUPPORTED = (() => {
  try {
    const probe = document.createElement("video");
    return (
      probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') !== "" ||
      probe.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') !== ""
    );
  } catch {
    return false;
  }
})();

/**
 * Bounds on trimming a segment down to its detections.
 *
 * Below the first, the clip is too short to be worth watching as one; below the second,
 * the trim saves so little that it only costs the user the rest of the segment.
 */
const MIN_CLIP_SECONDS = 8;
const MIN_CLIP_SAVING = 20;

/**
 * Group detections that fired at effectively the same moment.
 *
 * One arrival trips several sensors at once — motion, vehicle and person within the same
 * second — and drawing a 2px marker per detection would stack them into what looks like a
 * single one anyway. Grouping makes that explicit: the marker's tooltip names everything
 * that fired there, so a count of three detections over two markers is legible rather
 * than looking like a marker went missing.
 *
 * The threshold scales with the clip, because a second is a lot of a 20-second event and
 * nothing at all in a five-minute segment.
 */
export function clusterDetections(marks, duration) {
  const window = Math.max(1.5, duration * 0.01);
  const clusters = [];
  for (const mark of [...marks].sort((a, b) => a.offset - b.offset)) {
    const last = clusters[clusters.length - 1];
    if (last && mark.offset - last.offset <= window) {
      if (!last.kinds.includes(mark.kind)) last.kinds.push(mark.kind);
      continue;
    }
    clusters.push({ offset: mark.offset, kinds: [mark.kind] });
  }
  return clusters;
}

/**
 * Load the vendored FLV demuxer, once.
 *
 * The recorder serves FLV, which no browser plays natively but every current one can
 * demux through Media Source Extensions. Doing it here is what removes ffmpeg from the
 * server entirely: Home Assistant only forwards bytes.
 *
 * It ships with the integration rather than coming from a CDN, and resolves relative to
 * this module so the versioned static path busts it along with everything else.
 */
let demuxerPromise = null;
function loadDemuxer() {
  if (demuxerPromise) return demuxerPromise;
  demuxerPromise = new Promise((resolve, reject) => {
    if (window.mpegts) {
      resolve(window.mpegts);
      return;
    }
    const script = document.createElement("script");
    script.src = new URL("../vendor/mpegts.js", import.meta.url).href;
    script.onload = () =>
      window.mpegts ? resolve(window.mpegts) : reject(new Error("demuxer did not load"));
    script.onerror = () => reject(new Error("could not load the FLV demuxer"));
    document.head.append(script);
  });
  return demuxerPromise;
}

/** Whether this browser can demux in software at all. */
function canDemux(demuxer) {
  try {
    return Boolean(demuxer?.isSupported?.());
  } catch {
    return false;
  }
}

const STYLES = /* css */ `
:host { display: flex; flex-direction: column; height: 100%; background: var(--rv-surface); min-height: 0; }

.head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--rv-line);
  flex: 0 0 auto;
}
.head__body { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.head__title { font-size: 0.98rem; font-weight: 650; }
.head__sub { font-size: 0.8rem; color: var(--rv-text-dim); }
.chips { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 2px; }

.stage {
  position: relative;
  background: #000;
  flex: 1 1 auto;
  min-height: 200px;
  display: grid;
  place-items: center;
}
video { width: 100%; height: 100%; max-height: 100%; display: block; object-fit: contain; background: #000; }

.overlay {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #fff;
  text-align: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.45);
  gap: 10px;
}
.overlay[hidden] { display: none; }
.overlay__box { display: flex; flex-direction: column; align-items: center; gap: 10px; max-width: 40ch; }
.overlay__text { font-size: 0.88rem; line-height: 1.5; opacity: 0.9; }
/* A bare div left the spinner and Cancel stacked against the left edge. */
.overlay__action { display: flex; align-items: center; justify-content: center; gap: 12px; }
.overlay__action:empty { display: none; }

.big-play {
  width: 64px; height: 64px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.16);
  display: grid; place-items: center;
  color: #fff;
  backdrop-filter: blur(4px);
}
.big-play .icon { --mdc-icon-size: 34px; width: 34px; height: 34px; }
.big-play:hover { background: rgba(255, 255, 255, 0.26); }

/* ---------------------------------------------------------------- controls */

.controls { padding: 10px 14px 12px; flex: 0 0 auto; display: flex; flex-direction: column; gap: 8px; }

.track {
  position: relative;
  height: 26px;
  display: flex;
  align-items: center;
  cursor: pointer;
  touch-action: none;
}
.track[aria-disabled="true"] { cursor: default; }
.track__rail {
  position: absolute;
  left: 0; right: 0;
  height: 5px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--rv-text) 16%, transparent);
  overflow: hidden;
}
.track__buffer { position: absolute; inset: 0 auto 0 0; background: color-mix(in srgb, var(--rv-text) 14%, transparent); width: 0; }
.track__played { position: absolute; inset: 0 auto 0 0; background: var(--rv-accent); width: 0; }
.track__knob {
  position: absolute;
  width: 13px; height: 13px;
  border-radius: 50%;
  background: var(--rv-accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--rv-accent) 28%, transparent);
  transform: translateX(-50%);
  pointer-events: none;
}

/* One marker per detection, so the count in the note below matches the bar. */
.markers { position: absolute; inset: 0; pointer-events: none; }

/* The trigger marker: solid when exact, dashed when estimated, and coloured by what was
   detected — the same tones the row chips use, so a marker and its chip read as the same
   thing. The generic warn colour remains only for a marker that stands for "the trigger
   fired somewhere around here", which belongs to no single detection type. */
.marker {
  position: absolute;
  top: 2px;
  bottom: 2px;
  width: 0;
  border-left: 2px solid var(--marker-tone, var(--rv-warn));
  /* Hoverable, so the tooltip naming what fired here is actually reachable. Scrubbing
     still works through it: the track handles the pointer event and measures against its
     own rect, so which child was hit makes no difference. */
  pointer-events: auto;
}
/* A 2px line is not a hover target; this widens it without moving it. */
.marker::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: -6px;
  width: 13px;
}
.marker[data-exact="false"] { border-left-style: dashed; }
.marker::after {
  content: "";
  position: absolute;
  top: -3px;
  left: -4px;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--marker-tone, var(--rv-warn));
}

.marker[data-tone="person"] { --marker-tone: var(--rv-tone-person); }
.marker[data-tone="vehicle"] { --marker-tone: var(--rv-tone-vehicle); }
.marker[data-tone="animal"] { --marker-tone: var(--rv-tone-animal); }
.marker[data-tone="alert"] { --marker-tone: var(--rv-tone-alert); }
.marker[data-tone="motion"] { --marker-tone: var(--rv-tone-motion); }
.marker[data-tone="neutral"] { --marker-tone: var(--rv-tone-neutral); }

.times { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; font-variant-numeric: tabular-nums; color: var(--rv-text-dim); }

.buttons { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.select {
  font: inherit;
  font-size: 0.8rem;
  padding: 6px 8px;
  border-radius: var(--rv-radius-sm);
  border: 1px solid var(--rv-line);
  background: var(--rv-surface);
  color: inherit;
}

.preroll-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--rv-text-dim);
}
.preroll-note .swatch { width: 12px; border-top: 2px solid var(--marker-tone, var(--rv-warn)); }
.preroll-note .swatch[data-exact="false"] { border-top-style: dashed; }
.preroll-note .swatch[data-tone="person"] { --marker-tone: var(--rv-tone-person); }
.preroll-note .swatch[data-tone="vehicle"] { --marker-tone: var(--rv-tone-vehicle); }
.preroll-note .swatch[data-tone="animal"] { --marker-tone: var(--rv-tone-animal); }
.preroll-note .swatch[data-tone="alert"] { --marker-tone: var(--rv-tone-alert); }
.preroll-note .swatch[data-tone="motion"] { --marker-tone: var(--rv-tone-motion); }
.preroll-note .swatch[data-tone="neutral"] { --marker-tone: var(--rv-tone-neutral); }

.trim-btn { padding: 5px 10px; font-size: 0.78rem; white-space: nowrap; }
.trim-btn[hidden] { display: none; }

/* ------------------------------------------------------------- save-as menu */

.anchor { position: relative; }
.pop {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  z-index: 20;
  min-width: 240px;
  padding: 10px;
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  color: var(--rv-text);
  box-shadow: var(--rv-shadow-lifted);
  border: 1px solid var(--rv-line);
  animation: rv-enter 140ms var(--rv-ease) both;
}
.pop[hidden] { display: none; }
.pop__title { font-size: 0.8rem; font-weight: 600; padding: 2px 6px 8px; }
.pop__note {
  margin-top: 8px;
  padding: 8px 6px 2px;
  border-top: 1px solid var(--rv-line);
  font-size: 0.72rem;
  line-height: 1.45;
  color: var(--rv-text-dim);
}
.save-option {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px;
  border-radius: var(--rv-radius-sm);
  text-align: left;
  font-size: 0.88rem;
}
.save-option:hover { background: color-mix(in srgb, var(--rv-accent) 10%, transparent); }
.save-option__body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.save-option__hint { font-size: 0.72rem; color: var(--rv-text-dim); }

@media (max-width: 620px) {
  .times .sep, .head__sub { display: none; }
}
`;

export class EventPlayer extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
    this._event = null;
    this._stream = null;
    this._canSeek = true;
    this._seekProbe = null;
    this._dragging = false;
    // "stream" seeks server-side by time; "file" is a plain progressive download.
    this._mode = "stream";
    // Seconds into the recording that the current stream starts at. Displayed time is
    // this plus the element's own, because a seek reopens the stream from zero.
    this._timeOffset = 0;
    /** Exact detection moments inside the current clip, from the recorder. */
    this._detections = [];
    this._lead = 0;
    /**
     * The stretch of the recording this clip covers, in window-relative seconds, or null
     * for the whole thing. Set only for 24/7 footage with known detections; the scrub bar,
     * the markers and the stop at the end all work in these coordinates.
     */
    this._clip = null;
    /** Set when the user asked for the whole segment back, which sticks per clip. */
    this._untrimmed = false;
    /** True once playback reached the end of a trimmed clip and the stream was dropped. */
    this._atClipEnd = false;
    /** The active demuxer, torn down whenever the source changes. */
    this._player = null;
    /** The in-flight clip download, so it can be cancelled. */
    this._saving = null;

    this._onDocClick = (event) => {
      if (this._savePop?.hidden !== false) return;
      // Anywhere outside the menu closes it, including the video and the controls. Testing
      // against the whole player instead left it open while clicking around behind it.
      if (event.composedPath().includes(this._saveAnchor)) return;
      this._savePop.hidden = true;
    };
    this._onEscape = (event) => {
      if (event.key === "Escape" && this._savePop?.hidden === false) {
        // Swallowed here, or the panel would read it as "close the player".
        event.stopPropagation();
        this._savePop.hidden = true;
      }
    };
  }

  connectedCallback() {
    document.addEventListener("click", this._onDocClick);
    this.addEventListener("keydown", this._onEscape);
  }

  disconnectedCallback() {
    document.removeEventListener("click", this._onDocClick);
    this.removeEventListener("keydown", this._onEscape);
    this._saving?.abort();
  }

  set store(store) {
    this._store = store;
  }

  set api(api) {
    this._api = api;
  }

  /** Load an event, or clear the player when given null. */
  setEvent(event) {
    const sameEvent = this._event && event && this._event.id === event.id;
    this._event = event;
    if (!this._built) this._build();

    if (!event) {
      this._destroyPlayer();
      this._video.removeAttribute("src");
      this._video.load();
      return;
    }

    if (!sameEvent) {
      // Only when the clip really changed. This method is called again on every store
      // update — a search patch landing, detection counts arriving — and closing the menu
      // there made it vanish seemingly at random while a clip was playing.
      if (this._savePop) this._savePop.hidden = true;

      // Prefer the resolution the user last chose, when this clip has it.
      const preferred = this._preferredStream(event);
      this._stream = preferred;
      this._canSeek = true;
      this._mode = "stream";
      this._timeOffset = 0;
      this._detections = [];
      this._clip = null;
      this._untrimmed = false;
      this._atClipEnd = false;
      this._openAtEvent(event);
    }
    this._renderChrome();
  }

  /**
   * Open a clip, starting shortly before the detection rather than at the top.
   *
   * A camera recording only on events produces a clip that *is* the event, so the
   * detection sits a second or two in and the offset works out at zero — those keep
   * playing from the start, pre-record buffer intact. A camera recording 24/7 produces
   * long segments where the event can be anywhere, and this is what skips to it.
   */
  async _openAtEvent(event) {
    let seek = 0;
    try {
      const result = await this._api.detections({
        entryId: event.entry_id,
        channel: event.channel,
        start: event.start,
        end: event.end,
      });
      if (this._event?.id !== event.id) return;
      this._detections = result.detections || [];
      this._lead = result.lead ?? 0;
      this._clip = this._clipWindow(event, result);

      // Prefer a detection matching what the row is labelled with; fall back to the
      // earliest of any kind.
      const wanted = new Set(event.triggers || []);
      const relevant = this._detections.filter((d) => wanted.has(d.kind));
      const chosen = (relevant.length ? relevant : this._detections)[0];
      if (chosen && chosen.offset > this._lead) {
        seek = Math.floor(chosen.offset - this._lead);
      }
      // The playhead is placed independently of the clip bounds, so a lead reaching
      // further back than the clip starts would otherwise open outside it. Never on the
      // very end, which would open a clip that instantly stops.
      if (this._clip) {
        const last = Math.max(this._clip.start, this._clip.end - 1);
        seek = Math.floor(Math.min(Math.max(seek, this._clip.start), last));
      }
    } catch {
      // No recorder, no history, or it has been purged: start at the beginning, which
      // is the behaviour that already works for event-triggered recordings.
      this._detections = [];
      this._clip = null;
    }

    if (this._event?.id !== event.id) return;
    this._loadSource({ resetTime: true, seek });
    this._renderChrome();
  }

  /**
   * The stretch of a segment worth playing: the detections in it, plus a margin.
   *
   * Only for 24/7 footage. Where the camera records on events the recorder already cut
   * the clip to the event and wrote its pre-record buffer into the front of it, so
   * trimming again would cut into exactly the footage the panel exists to preserve.
   *
   * Returns null when there is nothing to gain — no detections, no room, or a window
   * already about as short as the trim would make it. A mode that changes nothing is
   * worse than no mode.
   */
  _clipWindow(event, result) {
    if (!event.continuous || this._mode !== "stream") return null;

    const duration = this._windowDuration();
    if (!(duration > 0)) return null;

    // Detections inside this window only: the backend looks slightly further back, so a
    // detection belonging to the previous segment can appear with a negative offset.
    const inside = this._detections.filter(
      (d) => d.offset < duration && (d.end_offset ?? d.offset) > 0
    );
    if (inside.length === 0) return null;

    const lead = result.clip_lead ?? 0;
    const tail = result.clip_tail ?? 0;
    const first = Math.min(...inside.map((d) => d.offset));
    const last = Math.max(...inside.map((d) => d.end_offset ?? d.offset));

    const start = Math.max(0, first - lead);
    const end = Math.min(duration, last + tail);
    if (end - start < MIN_CLIP_SECONDS) return null;
    if (duration - (end - start) < MIN_CLIP_SAVING) return null;

    return { start, end };
  }

  /**
   * The clip's bounds, or the whole recording when it is not trimmed.
   *
   * Never trimmed outside the streaming route: a progressive download hands over the whole
   * file, whose timeline is the file's rather than this row's, so window-relative
   * detection offsets do not apply to it. Falling back to that route therefore drops the
   * trim, which is why this is asked rather than stored.
   */
  _clipBounds() {
    if (this._clip && !this._untrimmed && this._mode === "stream") return this._clip;
    return { start: 0, end: this._windowDuration() };
  }

  /** Whether a trim is available at all, trimmed or not — what the button hangs off. */
  get _trimmable() {
    return Boolean(this._clip);
  }

  /**
   * Swap between the event and the whole segment it sits in.
   *
   * Stopping at the end of a trimmed clip tears the stream down, so going back to the
   * full segment there has to reopen it — and it reopens where the clip ended, which is
   * exactly "carry on past the event".
   */
  _toggleTrim() {
    this._untrimmed = !this._untrimmed;

    // Reopening is only needed when the playhead is not somewhere the new bar can show:
    // stopped at the end of the clip, or — going the other way — already past the event.
    const position = this._displayTime();
    const { start, end } = this._clipBounds();
    if (this._atClipEnd || position < start || position >= end) {
      this._atClipEnd = false;
      this._loadSource({
        resetTime: true,
        // Expanding carries on from where the clip ran out; collapsing rewinds to the event.
        seek: Math.floor(this._untrimmed ? Math.max(position, this._clip.end) : start),
      });
    }
    this._onTime();
    this._renderChrome();
  }

  /**
   * Low resolution when the clip has it, high otherwise.
   *
   * Not a choice any more. Low is H.264 on every Reolink NVR seen so far and starts far
   * faster, while high is often H.265 at 4608x1728 — slow to open, slow to decode, and
   * undecodable outside Safari. Offering both mostly gave people a way to make playback
   * worse.
   */
  _preferredStream(event) {
    const streams = [...(event.streams || []), ...(event.alternate_streams || [])];
    if (streams.includes("sub")) return "sub";
    return streams[0] || "sub";
  }

  // ------------------------------------------------------------------ building

  _build() {
    this._title = el("div", { class: "head__title truncate" });
    this._sub = el("div", { class: "head__sub" });
    this._chips = el("div", { class: "chips" });

    this._prev = el(
      "button",
      { class: "icon-btn", title: "Previous event", onclick: () => this._step(-1) },
      icon("mdi:chevron-up")
    );
    this._next = el(
      "button",
      { class: "icon-btn", title: "Next event", onclick: () => this._step(1) },
      icon("mdi:chevron-down")
    );
    this._close = el(
      "button",
      {
        class: "icon-btn",
        title: "Close",
        onclick: () =>
          this.dispatchEvent(new CustomEvent("close-player", { bubbles: true, composed: true })),
      },
      icon("mdi:close")
    );

    const head = el(
      "header",
      { class: "head" },
      el("div", { class: "head__body" }, this._title, this._sub, this._chips),
      this._prev,
      this._next,
      this._close
    );

    // --- video stage
    this._video = el("video", { playsinline: true, preload: "metadata" });
    this._video.addEventListener("loadedmetadata", () => this._onMetadata());
    this._video.addEventListener("timeupdate", () => this._onTime());
    this._video.addEventListener("progress", () => this._onTime());
    this._video.addEventListener("play", () => this._renderPlayState());
    this._video.addEventListener("pause", () => this._renderPlayState());
    this._video.addEventListener("ended", () => this._renderPlayState());
    this._video.addEventListener("error", () => this._onError());

    this._overlayText = el("div", { class: "overlay__text" });
    this._overlayIcon = icon("mdi:alert-circle-outline", "icon--lg");
    this._overlayAction = el("div", { class: "overlay__action" });
    this._overlay = el(
      "div",
      { class: "overlay", hidden: true },
      el("div", { class: "overlay__box" }, this._overlayIcon, this._overlayText, this._overlayAction)
    );

    this._stage = el("div", { class: "stage" }, this._video, this._overlay);

    // --- scrub bar
    this._buffer = el("div", { class: "track__buffer" });
    this._played = el("div", { class: "track__played" });
    this._knob = el("div", { class: "track__knob" });
    this._markers = el("div", { class: "markers" });
    this._track = el(
      "div",
      {
        class: "track",
        role: "slider",
        tabindex: "0",
        "aria-label": "Seek",
        onpointerdown: (pointerEvent) => this._onScrubStart(pointerEvent),
        onkeydown: (keyEvent) => this._onTrackKeydown(keyEvent),
      },
      el("div", { class: "track__rail" }, this._buffer, this._played),
      this._markers,
      this._knob
    );

    // --- buttons
    this._playBtn = el(
      "button",
      { class: "icon-btn", title: "Play", onclick: () => this._togglePlay() },
      (this._playIcon = icon("mdi:play"))
    );
    this._muteBtn = el(
      "button",
      { class: "icon-btn", title: "Mute", onclick: () => this._toggleMute() },
      (this._muteIcon = icon("mdi:volume-high"))
    );
    this._fullBtn = el(
      "button",
      { class: "icon-btn", title: "Full screen", onclick: () => this._toggleFullscreen() },
      icon("mdi:fullscreen")
    );
    // The recorder's own whole-file download, offered only on the progressive-download
    // route where it is what is playing anyway. Hidden otherwise: with nothing to point at
    // it was a button that did nothing, and it reads as a rival to Save beside it.
    this._downloadLink = el(
      "a",
      { class: "icon-btn", title: "Download the whole recording", download: "", hidden: true },
      icon("mdi:download")
    );
    this._saveBtn = el(
      "button",
      {
        class: "icon-btn",
        title: "Save this clip as MP4",
        onclick: () => this._toggleSaveMenu(),
      },
      icon("mdi:tray-arrow-down")
    );
    this._savePop = el("div", { class: "pop", hidden: true });
    this._saveAnchor = el("div", { class: "anchor" }, this._saveBtn, this._savePop);

    this._current = el("span", { text: "0:00" });
    this._total = el("span", { text: "0:00" });
    this._prerollNote = el("div", { class: "preroll-note" });
    // Only shown when a trim is actually in play, so it never invites a question about
    // segments that were never trimmed.
    this._trimBtn = el("button", {
      class: "btn btn--quiet trim-btn",
      hidden: true,
      onclick: () => this._toggleTrim(),
    });

    this._controls = el(
      "div",
      { class: "controls" },
      this._track,
      el(
        "div",
        { class: "buttons" },
        this._playBtn,
        this._muteBtn,
        el(
          "div",
          { class: "times" },
          this._current,
          el("span", { class: "sep", text: "/" }),
          this._total
        ),
        el("div", { class: "spacer" }),
        this._prerollNote,
        this._trimBtn,
        this._saveAnchor,
        this._downloadLink,
        this._fullBtn
      )
    );

    this.shadowRoot.append(head, this._stage, this._controls);
    this._built = true;
  }

  // ------------------------------------------------------------------- loading

  async _loadSource({ resetTime, seek = 0 }) {
    // Any previous demuxer must go before another source is attached.
    this._destroyPlayer();
    const event = this._event;
    if (!event) return;
    // May be absent: the panel only searches one resolution, and the rest are resolved
    // server-side from the event's time window when they are actually played.
    const file = event.files?.[this._stream] || {
      name: "",
      start_id: "",
      playback_id: "",
      offset: 0,
    };

    const resumeAt = resetTime ? seek : this._displayTime();
    this._showOverlay("mdi:progress-download", "Opening recording…", { spinner: true });

    if (this._mode === "stream") {
      await this._loadStream(file, resumeAt);
      return;
    }

    try {
      const { candidates } = await this._api.playbackUrl({
        entryId: event.entry_id,
        channel: event.channel,
        stream: this._stream,
        filename: file.name,
        startId: file.start_id,
        endId: file.end_id,
      });
      if (this._event?.id !== event.id) return; // switched away while loading

      this._downloadLink.setAttribute(
        "download",
        `${event.camera.replace(/[^\w-]+/g, "_")}_${event.start.slice(0, 19).replace(/[:T]/g, "-")}.mp4`
      );

      this._candidates = candidates;
      this._candidateIndex = 0;
      this._pendingSeek = resumeAt > 0 ? resumeAt : 0;
      this._applyCandidate();
    } catch (err) {
      if (this._event?.id !== event.id) return;
      this._showOverlay(
        "mdi:alert-circle-outline",
        err?.message || "Could not open this recording."
      );
    }
  }

  /** Seconds into the recording currently shown, across a time-shifted stream. */
  _displayTime() {
    return this._timeOffset + (this._video.currentTime || 0);
  }

  /**
   * Open the recording, starting `seek` seconds in.
   *
   * The recorder starts a new stream at that position, so the element always plays from 0
   * and the offset is added back for display. That is what makes scrubbing possible when
   * the recording itself cannot be seeked by byte.
   */
  async _loadStream(file, seek) {
    const event = this._event;
    let demuxer;
    try {
      demuxer = await loadDemuxer();
    } catch {
      demuxer = null;
    }

    if (!canDemux(demuxer)) {
      // No Media Source Extensions: fall back to the progressive download, which plays
      // but cannot seek.
      this._mode = "file";
      this._timeOffset = 0;
      this._loadSource({ resetTime: true });
      return;
    }

    try {
      const { url } = await this._api.streamUrl({
        entryId: event.entry_id,
        channel: event.channel,
        stream: this._stream,
        filename: file.name,
        // The recording's start, not this row's: a long recording holds several rows.
        startId: file.file_start_id || file.start_id,
        playbackId: file.playback_id,
        offset: file.offset || 0,
        start: event.start,
        end: event.end,
        seek,
      });
      if (this._event?.id !== event.id) return; // switched away while opening

      this._destroyPlayer();
      this._timeOffset = Math.max(0, Math.floor(seek));
      this._candidates = null;
      this._canSeek = true;
      this._downloadLink.removeAttribute("href");
      this._downloadLink.hidden = true;

      // Live-paced rather than a seekable file: the recorder sends at roughly real time
      // and reports no length, so the demuxer must not try to seek within it.
      const player = demuxer.createPlayer(
        { type: "flv", isLive: true, url },
        { enableStashBuffer: false, stashInitialSize: 128, lazyLoad: false }
      );
      this._player = player;
      player.attachMediaElement(this._video);
      player.on(demuxer.Events.ERROR, () => this._onError());
      player.load();
      this._hideOverlay();

      const attempt = player.play();
      if (attempt?.catch) {
        attempt.catch(() => this._showOverlay(null, "", { play: true }));
      }
    } catch (err) {
      if (this._event?.id !== event.id) return;
      this._mode = "file";
      this._timeOffset = 0;
      this._loadSource({ resetTime: true });
      // eslint-disable-next-line no-console
      console.warn("Reolink Stamina: stream unavailable, using download", err);
    }
  }

  /** Tear down the demuxer, so an abandoned stream stops being pulled. */
  _destroyPlayer() {
    const player = this._player;
    this._player = null;
    if (!player) return;
    try {
      player.pause();
      player.unload();
      player.detachMediaElement();
      player.destroy();
    } catch {
      // Already gone.
    }
  }

  /** Point the video element at the current candidate. */  /** Point the video element at the current candidate. */
  _applyCandidate() {
    const candidate = this._candidates?.[this._candidateIndex];
    if (!candidate) return;
    this._downloadLink.href = candidate.url;
    this._downloadLink.hidden = false;
    this._video.src = candidate.url;
    this._video.load();
    this._hideOverlay();

    // Start at the beginning so the pre-record buffer is never skipped.
    const attempt = this._video.play();
    if (attempt?.catch) {
      attempt.catch(() => {
        // Autoplay blocked: offer an explicit play button instead of failing.
        this._showOverlay(null, "", { play: true });
      });
    }
  }

  _showOverlay(iconName, text, { spinner = false, play = false, cancel = null } = {}) {
    this._overlay.hidden = false;
    this._overlayText.textContent = text;
    this._overlayAction.replaceChildren();
    this._overlayIcon.style.display = iconName ? "" : "none";
    if (iconName && this._overlayIcon.tagName.toLowerCase() === "ha-icon") {
      this._overlayIcon.setAttribute("icon", iconName);
    }
    if (spinner) {
      this._overlayAction.append(el("div", { class: "spinner" }));
    }
    if (cancel) {
      this._overlayAction.append(
        el("button", { class: "btn btn--quiet", text: "Cancel", onclick: cancel })
      );
    }
    if (play) {
      this._overlayAction.append(
        el(
          "button",
          { class: "big-play", title: "Play", onclick: () => this._togglePlay() },
          icon("mdi:play")
        )
      );
    }
  }

  /**
   * Update the overlay's wording without rebuilding it.
   *
   * Progress arrives several times a second, and `_showOverlay` replaces the spinner
   * element each time — which restarts its CSS animation from zero, so it sat there
   * apparently frozen for the entire download.
   */
  _setOverlayText(text) {
    this._overlayText.textContent = text;
  }

  _hideOverlay() {
    this._overlay.hidden = true;
  }

  _onError() {
    if (!this._video.getAttribute("src")) return;

    // The stream may fail well after the URL was handed over, since the recorder only
    // refuses once ffmpeg reads it. Fall back to the progressive download, which needs
    // nothing of the streaming path.
    if (this._mode === "stream") {
      this._mode = "file";
      this._timeOffset = 0;
      this._showOverlay("mdi:progress-download", "Stream unavailable, downloading instead…", {
        spinner: true,
      });
      this._loadSource({ resetTime: true });
      return;
    }

    // A decode failure means the bytes arrived fine and the codec is the problem, so
    // trying another request type is pointless — switch resolution instead.
    const mediaError = this._video.error;
    if (mediaError && (mediaError.code === 3 || mediaError.code === 4)) {
      const streams = this._event?.streams || [];
      if (this._stream !== "sub" && streams.includes("sub")) {
        this._stream = "sub";
        this._showOverlay(
          "mdi:video-switch-outline",
          "High resolution could not be decoded — it is H.265 at full sensor size. Switching to low resolution…",
          { spinner: true }
        );
        this._loadSource({ resetTime: false });
        return;
      }
      this._showOverlay(
        "mdi:video-off-outline",
        "This browser cannot decode this recording. Only the high-resolution stream exists for it, and Reolink encodes that as H.265 at full sensor size, which many browsers refuse — including Safari at 8 megapixels."
      );
      return;
    }

    // Recorders disagree about which request type they accept, so try the next one
    // before declaring the clip unplayable.
    if (this._candidates && this._candidateIndex < this._candidates.length - 1) {
      this._candidateIndex += 1;
      this._showOverlay("mdi:progress-download", "Trying another playback method…", {
        spinner: true,
      });
      this._applyCandidate();
      return;
    }

    // Name the route that failed. An identical message for every different cause made
    // four separate faults look like one unfixed bug.
    const detail = [
      `route: ${this._mode === "stream" ? "stream" : "download"}`,
      `quality: ${streamLabel(this._stream)}`,
      this._candidates?.[this._candidateIndex]?.vodType
        ? `method: ${this._candidates[this._candidateIndex].vodType}`
        : null,
      this._video.error ? `media error ${this._video.error.code}` : null,
    ]
      .filter(Boolean)
      .join(" · ");

    this._showOverlay(
      "mdi:video-off-outline",
      `The NVR did not return a playable video (${detail}). It may still be writing this recording, or the clip may exist only in the other resolution.`
    );
  }

  // ------------------------------------------------------------------ playback

  _togglePlay() {
    // The stream was dropped at the end of the clip, so there is nothing to resume:
    // pressing play means watch it again.
    if (this._atClipEnd) {
      this._atClipEnd = false;
      this._hideOverlay();
      this._loadSource({ resetTime: true, seek: Math.floor(this._clipBounds().start) });
      return;
    }
    if (this._video.paused) {
      this._hideOverlay();
      this._video.play().catch(() => {});
    } else {
      this._video.pause();
    }
  }

  _toggleMute() {
    this._video.muted = !this._video.muted;
    this._renderPlayState();
  }

  _toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      this._stage.requestFullscreen?.().catch(() => {});
    }
  }

  /** The whole recording this row covers, which is what times are measured against. */
  _windowDuration() {
    // While streaming, the element only ever holds the part it has been sent, and its
    // own duration grows as data arrives — which made the scrub bar start at a second
    // and stretch. The recording's real length comes from the search result.
    if (this._mode === "stream") return this._event?.duration || 0;
    const native = this._video.duration;
    if (Number.isFinite(native) && native > 0) return native;
    // The NVR does not always report a duration; the search result knows it.
    return this._event?.duration || 0;
  }

  /** What the scrub bar spans: the trimmed clip, or the whole recording. */
  _duration() {
    const { start, end } = this._clipBounds();
    return Math.max(0, end - start);
  }

  _onMetadata() {
    if (this._mode === "stream") {
      // None of the reasoning below applies. Seeking here means asking the recorder to
      // reopen the stream at another offset, so it always works, and the element's own
      // seekable range only ever covers what has arrived so far — judging by it disabled
      // the scrub bar permanently.
      this._canSeek = true;
      this._track.setAttribute("aria-disabled", "false");
      clearTimeout(this._seekProbe);
      this._pendingSeek = 0;
      this._onTime();
      this._renderChrome();
      return;
    }

    // For a progressive download, seeking only works if the NVR honours range requests.
    // Where it does not, the scrub bar is disabled rather than pretending to work.
    // Measured against the whole file, which is what this route hands over.
    const duration = this._windowDuration();
    const seekable = this._video.seekable;
    this._canSeek =
      Number.isFinite(this._video.duration) &&
      seekable.length > 0 &&
      seekable.end(seekable.length - 1) >= duration * 0.98;

    this._track.setAttribute("aria-disabled", String(!this._canSeek));

    if (this._pendingSeek && this._canSeek) {
      this._video.currentTime = Math.min(this._pendingSeek, duration);
    }
    this._pendingSeek = 0;
    this._onTime();
    this._renderChrome();
  }

  _onTime() {
    const { start: clipStart } = this._clipBounds();
    const duration = this._duration();
    // Everything on the bar is relative to where the clip starts, not the recording.
    const shown = this._displayTime() - clipStart;
    const ratio = duration > 0 ? Math.min(1, Math.max(0, shown / duration)) : 0;
    this._played.style.width = `${ratio * 100}%`;
    this._knob.style.left = `${ratio * 100}%`;
    this._current.textContent = formatClock(Math.max(0, shown));
    this._total.textContent = formatClock(duration);

    const buffered = this._video.buffered;
    if (buffered.length > 0 && duration > 0) {
      // Offset by where this stream began, or a seek would show the buffer back at zero.
      const end = this._timeOffset + buffered.end(buffered.length - 1) - clipStart;
      this._buffer.style.width = `${Math.min(1, Math.max(0, end / duration)) * 100}%`;
    }

    if (shown >= duration && duration > 0) this._onReachedEnd();
  }

  /**
   * A trimmed clip has to be stopped by us; the recorder would keep sending the rest of
   * the segment quite happily, and a 20-second event would run on for minutes.
   *
   * The stream is dropped rather than merely paused, because pausing the element does not
   * stop the demuxer pulling from the recorder.
   */
  _onReachedEnd() {
    if (this._atClipEnd || this._mode !== "stream") return;
    if (!this._clip || this._untrimmed) return;
    this._atClipEnd = true;
    this._video.pause();
    this._destroyPlayer();
    this._renderPlayState();
    this._renderChrome();
  }

  _renderPlayState() {
    if (this._playIcon.tagName.toLowerCase() === "ha-icon") {
      this._playIcon.setAttribute("icon", this._video.paused ? "mdi:play" : "mdi:pause");
      this._muteIcon.setAttribute("icon", this._video.muted ? "mdi:volume-off" : "mdi:volume-high");
    }
    this._playBtn.title = this._video.paused ? "Play" : "Pause";
  }

  // ---------------------------------------------------------------- scrubbing

  _seekToRatio(ratio) {
    if (!this._canSeek) return;
    const duration = this._duration();
    if (duration <= 0) return;
    // The bar spans the clip, so a ratio along it lands at an offset into the recording.
    const { start: clipStart } = this._clipBounds();

    if (this._mode === "stream") {
      // Server-side, time-based seeking: the recorder starts a new stream there.
      const target = clipStart + Math.max(0, Math.min(duration - 1, ratio * duration));
      // Same fallback as loading: a resolution that was never searched has no file name
      // here, and the backend resolves it from the event's time window instead.
      const file = this._event?.files?.[this._stream] || {
        name: "",
        start_id: "",
        playback_id: "",
        offset: 0,
      };
      // Seeking backwards out of a finished clip is how you rewatch it.
      this._atClipEnd = false;
      this._showOverlay("mdi:progress-clock", "Seeking…", { spinner: true });
      this._loadStream(file, target);
      return;
    }

    const target = clipStart + Math.max(0, Math.min(duration, ratio * duration));
    const before = this._video.currentTime;
    this._video.currentTime = target;

    // Verify the seek actually took: some Reolink firmware accepts the request and
    // then ignores it. One check is enough to stop offering a control that lies.
    clearTimeout(this._seekProbe);
    this._seekProbe = setTimeout(() => {
      if (Math.abs(this._video.currentTime - before) < 0.25 && Math.abs(target - before) > 1) {
        this._canSeek = false;
        this._track.setAttribute("aria-disabled", "true");
      }
    }, 1500);
  }

  _ratioFromPointer(clientX) {
    const rect = this._track.getBoundingClientRect();
    return rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
  }

  _onScrubStart(pointerEvent) {
    if (!this._canSeek) return;
    pointerEvent.preventDefault();
    this._track.setPointerCapture(pointerEvent.pointerId);
    this._dragging = true;
    this._seekToRatio(this._ratioFromPointer(pointerEvent.clientX));

    const onMove = (moveEvent) => {
      if (!this._dragging) return;
      this._seekToRatio(this._ratioFromPointer(moveEvent.clientX));
    };
    const onUp = () => {
      this._dragging = false;
      this._track.removeEventListener("pointermove", onMove);
      this._track.removeEventListener("pointerup", onUp);
      this._track.removeEventListener("pointercancel", onUp);
    };
    this._track.addEventListener("pointermove", onMove);
    this._track.addEventListener("pointerup", onUp);
    this._track.addEventListener("pointercancel", onUp);
  }

  _onTrackKeydown(keyEvent) {
    const step = keyEvent.shiftKey ? 10 : 2;
    // Stepping is along the bar, so it works in the clip's coordinates like the bar does.
    const { start: clipStart } = this._clipBounds();
    const within = this._displayTime() - clipStart;
    if (keyEvent.key === "ArrowRight") {
      keyEvent.preventDefault();
      this._seekToRatio((within + step) / Math.max(1, this._duration()));
    } else if (keyEvent.key === "ArrowLeft") {
      keyEvent.preventDefault();
      this._seekToRatio((within - step) / Math.max(1, this._duration()));
    } else if (keyEvent.key === " ") {
      keyEvent.preventDefault();
      this._togglePlay();
    }
  }


  _step(delta) {
    this.dispatchEvent(
      new CustomEvent("step-event", { detail: delta, bubbles: true, composed: true })
    );
  }

  // ------------------------------------------------------------------- saving

  _toggleSaveMenu() {
    this._savePop.hidden = !this._savePop.hidden;
    if (!this._savePop.hidden) this._renderSaveMenu();
  }

  /**
   * Offer every resolution the clip exists in.
   *
   * Whichever is chosen, what gets written is the clip on screen — trimmed if the bar is
   * trimmed, the whole segment if not — never the half-hour recording it lives in.
   */
  _renderSaveMenu() {
    const event = this._event;
    if (!event) return;
    const streams = [...(event.streams || []), ...(event.alternate_streams || [])];
    const length = this._duration();

    this._savePop.replaceChildren(
      el("div", { class: "pop__title" }, `Save ${formatClock(length)} as MP4`),
      ...streams.map((stream) => {
        // High resolution is H.265 on every Reolink recorder seen so far, and Apple's own
        // players will not open the H.265 these NVRs produce: the file is valid and plays in
        // VLC and in browsers, but QuickTime and Preview load it forever. Their encoder
        // leaves the stream's scan-type and constraint flags unset, and nothing about how the
        // file is packaged changes that — ffmpeg's own remux of the same footage stalls
        // identically. Better said here than discovered after a long download.
        const hevc = stream === "main";
        return el(
          "button",
          {
            class: "save-option",
            onclick: () => {
              this._savePop.hidden = true;
              this._saveClip(stream);
            },
          },
          icon(hevc ? "mdi:high-definition" : "mdi:standard-definition"),
          el(
            "span",
            { class: "save-option__body" },
            el("span", { text: streamLabel(stream) }),
            el("span", {
              class: "save-option__hint",
              text: hevc ? "H.265 — VLC or a browser, not QuickTime" : "H.264 — plays anywhere",
            })
          )
        );
      }),
      el("div", {
        class: "pop__note",
        // The recorder streams a recording at roughly the speed it was filmed, and there is
        // no faster route that can also start partway in. Saying so beats a silent wait.
        text: `Copied, not re-encoded — the recorder sends it at about real time, so this takes
          roughly ${formatClock(length)}.`.replace(/\s+/g, " "),
      })
    );
  }

  /**
   * Save the current clip.
   *
   * The recorder can cut a clip itself, given a start and an end — so the first thing tried
   * is simply asking it to, and handing the browser a link to the result. That is a plain
   * download of a plain MP4: nothing is remuxed, nothing is held in memory, and it arrives
   * at network speed rather than at the speed the footage was filmed.
   *
   * Where a device will not do that, the clip is assembled here from the playback stream
   * instead, which is what this panel did before and what still covers hubs and older
   * firmware.
   */
  async _saveClip(stream) {
    const event = this._event;
    if (!event || this._saving) return;

    const stamp = event.start.slice(0, 19).replace(/[:T]/g, "-");
    const name = `${event.camera.replace(/[^\w-]+/g, "_")}_${stamp}_${stream}.mp4`;
    this._video.pause();
    await this._saveClipFromStream(stream, name);
  }

  /**
   * Read the playback stream and build the MP4 here.
   *
   * Asking the recorder to cut the clip instead would be faster and simpler — and it does
   * have a command for it — but see fragments.py: on the recorders tested it prepares a
   * fragment it then refuses to serve. Until that changes, this is the route that works.
   */
  async _saveClipFromStream(stream, name) {
    const event = this._event;
    const { start, end } = this._clipBounds();
    const seconds = Math.max(1, Math.round(end - start));
    const file = event.files?.[stream] || { name: "", start_id: "", playback_id: "", offset: 0 };

    this._saving = new AbortController();
    const cancel = () => this._saving?.abort();
    this._showOverlay("mdi:tray-arrow-down", "Assembling clip… 0%", { spinner: true, cancel });

    try {
      const { url } = await this._api.streamUrl({
        entryId: event.entry_id,
        channel: event.channel,
        stream,
        filename: file.name,
        startId: file.file_start_id || file.start_id,
        playbackId: file.playback_id,
        offset: file.offset || 0,
        start: event.start,
        end: event.end,
        seek: Math.floor(start),
      });

      const blob = await downloadClip(url, {
        seconds,
        maxBytes: MAX_CLIP_BYTES,
        signal: this._saving.signal,
        onProgress: ({ seconds: reached }) => {
          const share = Math.min(100, Math.round((reached / seconds) * 100));
          this._setOverlayText(`Assembling clip… ${share}%`);
        },
      });

      if (this._event?.id !== event.id) return; // moved on while it was being written
      saveBlob(blob, name);
      this._hideOverlay();
    } catch (error) {
      if (error?.name === "AbortError") {
        this._hideOverlay();
      } else {
        this._showOverlay(
          "mdi:alert-circle-outline",
          `Could not save this clip: ${error?.message || error}`
        );
      }
    } finally {
      this._saving = null;
    }
  }

  // -------------------------------------------------------------------- chrome

  /**
   * Offer the whole segment when the clip was cut down, and the cut clip when it wasn't.
   *
   * The button states what you get by pressing it, and says how long that is, because
   * "5:00" against a clip reading "0:48" is the only explanation the trim needs.
   */
  _renderTrimButton() {
    this._trimBtn.hidden = !this._trimmable || this._mode !== "stream";
    if (this._trimBtn.hidden) return;

    const whole = this._windowDuration();
    const event = Math.max(0, this._clip.end - this._clip.start);
    this._trimBtn.replaceChildren(
      icon(this._untrimmed ? "mdi:arrow-collapse-horizontal" : "mdi:arrow-expand-horizontal"),
      el("span", {
        text: this._untrimmed
          ? `Event only (${formatClock(event)})`
          : `Full segment (${formatClock(whole)})`,
      })
    );
    this._trimBtn.title = this._untrimmed
      ? "Trim back to the detections in this segment"
      : "Play the whole recording segment, not just the event";
  }

  _renderChrome() {
    const event = this._event;
    if (!event) return;

    this._renderTrimButton();
    // Nothing to write a file from when the recorder listed no resolution for this row.
    this._saveBtn.hidden = !(event.streams?.length || event.alternate_streams?.length);
    // The menu quotes the clip's length and how long saving it will take, so an open one is
    // rebuilt whenever anything it quotes may have moved under it.
    if (this._savePop.hidden === false) this._renderSaveMenu();
    this._title.textContent = event.camera;
    this._sub.textContent = `${event.nvr} · ${formatTime(event.start)} · ${formatSize(event.size)}`;

    const triggers = sortTriggers(event.triggers || []);
    this._chips.replaceChildren(
      ...(triggers.length
        ? triggers.map((trigger) => {
            const meta = triggerMeta(trigger);
            return el("span", { class: "chip", dataset: { tone: meta.tone } }, icon(meta.icon), meta.label);
          })
        : [el("span", { class: "chip", dataset: { tone: "neutral" } }, "Recording")])
    );

    // Real detection times beat an estimated pre-roll: mark each one exactly.
    if (this._detections.length > 0 && this._duration() > 0) {
      const duration = this._duration();
      const { start: clipStart, end: clipEnd } = this._clipBounds();
      // Anything still detecting when the clip opens is marked at its start rather than
      // dropped: an event that began just before the segment boundary is the whole reason
      // this segment is tagged, and it is on screen from the first frame.
      const marks = this._detections
        .filter((d) => (d.end_offset ?? d.offset) > clipStart && d.offset < clipEnd)
        .map((d) => ({ ...d, offset: Math.max(d.offset, clipStart) }));
      if (marks.length > 0) {
        const clusters = clusterDetections(marks, duration);
        this._markers.replaceChildren(
          ...clusters.map((cluster) => {
            // Coloured by the most significant thing that fired here, which is the same
            // rule the row's leading icon uses: a car arriving is a vehicle, not motion.
            const meta = triggerMeta(sortTriggers(cluster.kinds)[0]);
            return el("div", {
              class: "marker",
              dataset: { exact: "true", tone: meta.tone },
              style: { left: `${((cluster.offset - clipStart) / duration) * 100}%` },
              title: `${cluster.kinds.map((kind) => triggerMeta(kind).label).join(", ")} detected ${Math.round(cluster.offset)}s into the recording`,
            });
          })
        );
        const overall = triggerMeta(sortTriggers(marks.map((d) => d.kind))[0]);
        this._prerollNote.replaceChildren(
          el("span", { class: "swatch", dataset: { exact: "true", tone: overall.tone } }),
          el("span", {
            text: marks.length > 1 ? `${marks.length} detections` : `${overall.label} detected`,
          })
        );
        this._prev.disabled = !this._store?.hasAdjacentEvent(-1);
        this._next.disabled = !this._store?.hasAdjacentEvent(1);
        this._renderPlayState();
        return;
      }
    }

    // Trigger marker
    const preRoll = event.pre_roll || { seconds: 0, exact: false };
    const duration = this._duration();
    if (duration > 0 && preRoll.seconds > 0 && preRoll.seconds < duration) {
      // No detection to attribute this to, so it takes the tone of whatever the recorder
      // tagged the recording with.
      const tone = triggers.length ? triggerMeta(triggers[0]).tone : "neutral";
      this._markers.replaceChildren(
        el("div", {
          class: "marker",
          dataset: { exact: String(Boolean(preRoll.exact)), tone },
          style: { left: `${(preRoll.seconds / duration) * 100}%` },
          title: preRoll.exact
            ? `Trigger fired ${preRoll.seconds}s in (pre-record buffer before this point)`
            : `Trigger fired around ${preRoll.seconds}s in (estimated — this camera does not report its pre-record time)`,
        })
      );
      this._prerollNote.replaceChildren(
        el("span", { class: "swatch", dataset: { exact: String(Boolean(preRoll.exact)) } }),
        el("span", { text: preRoll.exact ? "trigger" : "trigger (est.)" })
      );
    } else {
      this._markers.replaceChildren();
      this._prerollNote.replaceChildren();
    }

    this._prev.disabled = !this._store?.hasAdjacentEvent(-1);
    this._next.disabled = !this._store?.hasAdjacentEvent(1);
    this._renderPlayState();
  }

  /** Re-read state that depends on the store, such as prev/next availability. */
  syncFromStore() {
    if (this._event) this._renderChrome();
  }
}

// Guarded: Home Assistant may import this module more than once (after an
// update, or from a cached copy), and a duplicate define() throws and takes the
// whole panel down with it.
if (!customElements.get("reolink-event-player")) customElements.define("reolink-event-player", EventPlayer);
