/**
 * The scrub bar, its buffer, and the detection markers on it.
 *
 * Custom rather than the browser's own control bar, because a native one cannot be overlaid
 * with a marker for where the trigger fired — which is the whole reason the panel draws its
 * own controls.
 *
 * It knows nothing about routes, clips or detections: it is given a position, a length and
 * a list of marks already placed as fractions along itself, and it reports back where the
 * user pointed. Everything about *what* those positions mean stays in the player.
 */

import { el } from "../dom.js";

export const SCRUB_STYLES = /* css */ `
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

/*
 * A detection the model found unusual keeps its whole marker — line and dot — in the colour
 * of whatever was detected, and the dot is circled in red.
 *
 * The dot used to turn red, which meant the mark could only carry one of the two facts: a red
 * dot said "unusual" and stopped saying "person", and on a vehicle it overwrote the tone the
 * rest of the panel uses for vehicles. A soft band behind the whole marker carried both but
 * read as a smudge at this size. A ring is the third try and the right one: it is a shape
 * rather than a wash, it survives being two pixels wide, and it leaves the fill alone.
 */
.marker[data-unusual="true"]::after {
  /* Circled, not recoloured. The dot keeps the tone that says what was detected and gains a
     ring around it, held off the fill by a gap in the bar's own colour so the two do not
     blend into one bigger dot — the same trick that keeps a marker legible where it crosses
     a line. A ring reads as "this one" at a glance and costs the mark nothing. */
  box-shadow:
    0 0 0 1.5px var(--rv-surface),
    0 0 0 3px var(--rv-tone-alert);
}
`;

export class ScrubBar {
  /**
   * `onSeek` is called with a fraction of the bar; `onTogglePlay` with nothing.
   *
   * Both arrive from a pointer or from the keyboard, and neither is throttled: dragging
   * reopens the stream server-side, which is the player's business to pace, not this one's.
   */
  constructor({ onSeek, onTogglePlay }) {
    this._onSeek = onSeek;
    this._onTogglePlay = onTogglePlay;
    this._enabled = true;
    this._dragging = false;
    this._duration = 0;

    this._buffer = el("div", { class: "track__buffer" });
    this._played = el("div", { class: "track__played" });
    this._knob = el("div", { class: "track__knob" });
    this._markers = el("div", { class: "markers" });

    this.element = el(
      "div",
      {
        class: "track",
        role: "slider",
        tabindex: "0",
        "aria-label": "Seek",
        onpointerdown: (pointerEvent) => this._onPointerDown(pointerEvent),
        onkeydown: (keyEvent) => this._onKeydown(keyEvent),
      },
      el("div", { class: "track__rail" }, this._buffer, this._played),
      this._markers,
      this._knob
    );
  }

  /** Whether the bar accepts a seek at all; a download the device will not range-request cannot. */
  setEnabled(enabled) {
    this._enabled = Boolean(enabled);
    this.element.setAttribute("aria-disabled", String(!this._enabled));
  }

  get enabled() {
    return this._enabled;
  }

  /** Where the playhead is, how long the clip is, and how much has arrived — all in seconds. */
  update({ position, duration, buffered }) {
    this._duration = duration;
    const ratio = duration > 0 ? Math.min(1, Math.max(0, position / duration)) : 0;
    this._played.style.width = `${ratio * 100}%`;
    this._knob.style.left = `${ratio * 100}%`;
    if (buffered !== null && buffered !== undefined && duration > 0) {
      this._buffer.style.width = `${Math.min(1, Math.max(0, buffered / duration)) * 100}%`;
    }
  }

  /**
   * Put the bar back to the start, with nothing played, buffered or marked.
   *
   * Called the moment the clip changes rather than waiting for the new one to report a
   * time. That report is the first `timeupdate`, which on any route that has to open a
   * stream server-side is several seconds away — and until it arrives the bar would go on
   * showing how far through the *previous* clip the viewer had got, on a clip that is not
   * playing yet. `update` then moves it to wherever playback actually begins.
   */
  reset() {
    this._duration = 0;
    this._played.style.width = "0%";
    this._knob.style.left = "0%";
    // Zeroed here rather than left to `update`, which deliberately leaves the buffer alone
    // when there is nothing to report — so it would otherwise keep the width the previous
    // clip left it at, for as long as the new one has told us nothing.
    this._buffer.style.width = "0%";
    this._markers.replaceChildren();
  }

  /** Marks as `{ratio, tone, exact, unusual, title}`, already placed along the bar. */
  setMarkers(marks) {
    this._markers.replaceChildren(
      ...marks.map((mark) =>
        el("div", {
          class: "marker",
          dataset: {
            exact: String(Boolean(mark.exact)),
            tone: mark.tone,
            unusual: String(Boolean(mark.unusual)),
          },
          style: { left: `${mark.ratio * 100}%` },
          title: mark.title,
        })
      )
    );
  }

  _ratioFromPointer(clientX) {
    const rect = this.element.getBoundingClientRect();
    return rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
  }

  _onPointerDown(pointerEvent) {
    if (!this._enabled) return;
    pointerEvent.preventDefault();
    this.element.setPointerCapture(pointerEvent.pointerId);
    this._dragging = true;
    this._onSeek(this._ratioFromPointer(pointerEvent.clientX));

    const onMove = (moveEvent) => {
      if (!this._dragging) return;
      this._onSeek(this._ratioFromPointer(moveEvent.clientX));
    };
    const onUp = () => {
      this._dragging = false;
      this.element.removeEventListener("pointermove", onMove);
      this.element.removeEventListener("pointerup", onUp);
      this.element.removeEventListener("pointercancel", onUp);
    };
    this.element.addEventListener("pointermove", onMove);
    this.element.addEventListener("pointerup", onUp);
    this.element.addEventListener("pointercancel", onUp);
  }

  /**
   * Arrow keys step along the bar, space plays.
   *
   * Stepping is expressed as a fraction like everything else here, so the player does not
   * have to know that the bar was what moved.
   */
  _onKeydown(keyEvent) {
    const step = keyEvent.shiftKey ? 10 : 2;
    const duration = Math.max(1, this._duration);
    const current = Number.parseFloat(this._played.style.width) / 100 || 0;
    if (keyEvent.key === "ArrowRight") {
      keyEvent.preventDefault();
      this._onSeek(current + step / duration);
    } else if (keyEvent.key === "ArrowLeft") {
      keyEvent.preventDefault();
      this._onSeek(current - step / duration);
    } else if (keyEvent.key === " ") {
      keyEvent.preventDefault();
      this._onTogglePlay();
    }
  }
}
