/**
 * Pinch, drag and double-tap to look closer at the picture.
 *
 * Home Assistant's frontend ships `user-scalable=no` in its viewport, so the browser's own
 * pinch does nothing anywhere inside it — and on a phone, getting closer to a face or a
 * number plate is most of what someone opens a clip for. So the gesture is implemented here.
 * Doing it ourselves is better than the browser's page zoom would have been anyway: it zooms
 * the picture rather than the panel, so the controls stay put and stay the size they were.
 *
 * Nothing is ever resized — only transformed — so decoding, layout and the route the clip
 * arrived by are all untouched by it.
 *
 * Panning is clamped against the picture rather than the video element, because the element
 * is letterboxed: a 16:9 recording in a taller box has black above and below it, and being
 * able to drag the picture off screen to look at that black is not a feature.
 */

import { el, icon } from "../dom.js";

export const ZOOM_STYLES = /* css */ `
/* The gesture is ours, so the browser must not also treat a drag here as a scroll. */
.stage { touch-action: none; overflow: hidden; }
video { transform-origin: 0 0; }

.zoom-pill {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 3;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  border-radius: var(--rv-radius-pill);
  /* Solid rather than a blurred backdrop: this sits over a picture that repaints every
     frame, so a blur here would be re-run every frame for the whole time it is on screen.
     Over black at 62% there is nothing to see through it anyway. */
  background: rgba(0, 0, 0, 0.62);
  color: #fff;
  font-size: 0.76rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  animation: rv-enter 160ms var(--rv-ease) both;
}
.zoom-pill[hidden] { display: none; }
.zoom-pill .icon { --mdc-icon-size: 15px; width: 15px; height: 15px; }
`;

const MIN_SCALE = 1;
const MAX_SCALE = 8;
/** Where a double-tap lands, when it is not undoing a zoom. */
const TAP_SCALE = 2.5;
const DOUBLE_TAP_MS = 320;
/** How far a tap may wander and still be a tap rather than a drag. */
const TAP_SLOP = 14;

export class PinchZoom {
  /**
   * `container` is what the gesture is measured against and `target` is what moves; they
   * are expected to share an origin, which the stage and the video element do. `onChange`
   * is called whenever the zoom level changes, so the player can say so in its chrome.
   */
  constructor({ container, target, onChange = null }) {
    this._container = container;
    this._target = target;
    this._onChange = onChange;
    this._scale = 1;
    this._x = 0;
    this._y = 0;
    /** Live pointers by id, so a second finger turns a drag into a pinch mid-gesture. */
    this._pointers = new Map();
    this._gesture = null;
    this._lastTap = null;

    // Tapping it is the way back out, which matters most on a phone: pinching back down to
    // exactly 1x by hand is fiddly, and there is no keyboard to press Escape on.
    this.indicator = el(
      "button",
      {
        class: "zoom-pill",
        hidden: true,
        title: "Reset zoom",
        onclick: () => this.reset(),
        // The stage would otherwise read the press on it as the start of a pan.
        onpointerdown: (event) => event.stopPropagation(),
      },
      icon("mdi:magnify-minus-outline"),
      (this._level = el("span", { text: "1.0×" }))
    );

    this._onPointerDown = (event) => this._pointerDown(event);
    this._onPointerMove = (event) => this._pointerMove(event);
    this._onPointerUp = (event) => this._pointerUp(event);
    this._onWheel = (event) => this._wheel(event);
    // The picture's shape decides how far it may be panned, and both the box it sits in and
    // the recording in it can change under a zoom that is already applied.
    this._observer =
      typeof ResizeObserver === "function" ? new ResizeObserver(() => this._apply()) : null;
  }

  get zoomed() {
    return this._scale > 1.01;
  }

  attach() {
    this._container.addEventListener("pointerdown", this._onPointerDown);
    this._container.addEventListener("pointermove", this._onPointerMove);
    this._container.addEventListener("pointerup", this._onPointerUp);
    this._container.addEventListener("pointercancel", this._onPointerUp);
    // Not passive: a trackpad pinch arrives as a ctrl-wheel, and letting it through would
    // zoom the whole Home Assistant page instead of the clip.
    this._container.addEventListener("wheel", this._onWheel, { passive: false });
    this._observer?.observe(this._container);
  }

  detach() {
    this._container.removeEventListener("pointerdown", this._onPointerDown);
    this._container.removeEventListener("pointermove", this._onPointerMove);
    this._container.removeEventListener("pointerup", this._onPointerUp);
    this._container.removeEventListener("pointercancel", this._onPointerUp);
    this._container.removeEventListener("wheel", this._onWheel);
    this._observer?.disconnect();
  }

  /** Back to the whole picture — a new clip, or a new resolution, starts unzoomed. */
  reset() {
    this._pointers.clear();
    this._gesture = null;
    this._scale = 1;
    this._x = 0;
    this._y = 0;
    this._apply();
  }

  /** Re-clamp against a picture whose shape or box has changed, keeping the zoom level. */
  refresh() {
    this._apply();
  }

  // ------------------------------------------------------------------ geometry

  /**
   * Where the picture actually is inside the video element.
   *
   * `object-fit: contain` letterboxes it, and until the first frame has been decoded there
   * is nothing to measure — in which case the whole element stands in, which is what the
   * unzoomed state needs anyway.
   */
  _picture() {
    const width = this._target.clientWidth;
    const height = this._target.clientHeight;
    const videoWidth = this._target.videoWidth;
    const videoHeight = this._target.videoHeight;
    if (!videoWidth || !videoHeight || !width || !height) {
      return { left: 0, top: 0, width, height };
    }
    const fit = Math.min(width / videoWidth, height / videoHeight);
    const shown = { width: videoWidth * fit, height: videoHeight * fit };
    return {
      left: (width - shown.width) / 2,
      top: (height - shown.height) / 2,
      ...shown,
    };
  }

  /**
   * Keep the picture covering the view, or centred in it when it is smaller than the view.
   *
   * The second case is what holds a zoomed-out picture in the middle of the stage and what
   * centres the short axis of a wide recording no matter how far it has been dragged.
   */
  _clamped(x, y, scale) {
    const picture = this._picture();
    const axis = (offset, start, length, view) => {
      const from = start * scale;
      const size = length * scale;
      if (size <= view) return view / 2 - (start + length / 2) * scale;
      return Math.min(-from, Math.max(view - from - size, offset));
    };
    return {
      x: axis(x, picture.left, picture.width, this._target.clientWidth),
      y: axis(y, picture.top, picture.height, this._target.clientHeight),
    };
  }

  _apply() {
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, this._scale));
    const { x, y } = this._clamped(this._x, this._y, scale);
    this._scale = scale;
    this._x = x;
    this._y = y;
    this._target.style.transform =
      scale > 1 ? `translate(${x}px, ${y}px) scale(${scale})` : "";
    this.indicator.hidden = !this.zoomed;
    this._level.textContent = `${scale.toFixed(1)}×`;
    this._onChange?.(scale);
  }

  /** A client point in the container's own coordinates, which the video shares. */
  _local(clientX, clientY) {
    const rect = this._container.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  /** Zoom about a fixed point, so what is under the fingers stays under them. */
  _zoomAbout(point, scale) {
    const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
    // The content coordinate currently under the point has to end up there again.
    const content = { x: (point.x - this._x) / this._scale, y: (point.y - this._y) / this._scale };
    this._scale = next;
    this._x = point.x - content.x * next;
    this._y = point.y - content.y * next;
    this._apply();
  }

  // ------------------------------------------------------------------ gestures

  _midpoint() {
    const [a, b] = [...this._pointers.values()];
    return this._local((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2);
  }

  _spread() {
    const [a, b] = [...this._pointers.values()];
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  }

  _pointerDown(event) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    this._pointers.set(event.pointerId, event);

    if (this._pointers.size === 2) {
      // Whatever the first finger was doing, two fingers mean a pinch from here.
      this._gesture = {
        kind: "pinch",
        spread: this._spread(),
        point: this._midpoint(),
        scale: this._scale,
        x0: this._x,
        y0: this._y,
        moved: true,
      };
      return;
    }
    if (this._pointers.size === 1) {
      const point = this._local(event.clientX, event.clientY);
      this._gesture = { kind: "drag", point, x: this._x, y: this._y, moved: false };
    }
  }

  _pointerMove(event) {
    if (!this._pointers.has(event.pointerId)) return;
    this._pointers.set(event.pointerId, event);
    const gesture = this._gesture;
    if (!gesture) return;

    if (gesture.kind === "pinch" && this._pointers.size >= 2) {
      event.preventDefault();
      const spread = this._spread();
      if (gesture.spread <= 0) return;
      // Anchored on the live midpoint rather than the one the pinch started at, so moving
      // both fingers together pans while they pinch — which is how a photo viewer behaves.
      const content = {
        x: (gesture.point.x - gesture.x0) / gesture.scale,
        y: (gesture.point.y - gesture.y0) / gesture.scale,
      };
      const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, (gesture.scale * spread) / gesture.spread));
      const now = this._midpoint();
      this._scale = next;
      this._x = now.x - content.x * next;
      this._y = now.y - content.y * next;
      this._apply();
      return;
    }

    if (gesture.kind !== "drag") return;
    const point = this._local(event.clientX, event.clientY);
    const travelled = Math.hypot(point.x - gesture.point.x, point.y - gesture.point.y);
    if (!gesture.moved && travelled < TAP_SLOP) return;
    // Only a zoomed picture has anywhere to go; an unzoomed one lets the press through to
    // whatever is under it, so the overlay's own buttons keep working.
    if (!this.zoomed) return;
    if (!gesture.moved) {
      gesture.moved = true;
      // Taken now rather than on pointerdown: capturing a press that turns out to be a tap
      // would swallow the click it was going to become.
      this._container.setPointerCapture?.(event.pointerId);
    }
    event.preventDefault();
    this._x = gesture.x + (point.x - gesture.point.x);
    this._y = gesture.y + (point.y - gesture.point.y);
    this._apply();
  }

  _pointerUp(event) {
    const gesture = this._gesture;
    this._pointers.delete(event.pointerId);

    if (this._pointers.size === 1 && gesture?.kind === "pinch") {
      // One finger lifted out of a pinch: carry on as a drag from where the other one is.
      const [remaining] = [...this._pointers.values()];
      const point = this._local(remaining.clientX, remaining.clientY);
      this._gesture = { kind: "drag", point, x: this._x, y: this._y, moved: true };
      return;
    }
    if (this._pointers.size > 0) return;
    this._gesture = null;

    if (gesture?.kind === "drag" && !gesture.moved) this._tap(event);
  }

  /**
   * Two taps in the same place, close together: in, or back out if already in.
   *
   * A single tap is left alone. The stage is what the overlay's messages and its play button
   * sit on, and swallowing presses on them to serve a gesture nobody made would be a poor
   * trade.
   */
  _tap(event) {
    const point = this._local(event.clientX, event.clientY);
    const previous = this._lastTap;
    const now = performance.now();
    if (
      previous &&
      now - previous.at < DOUBLE_TAP_MS &&
      Math.hypot(point.x - previous.x, point.y - previous.y) < TAP_SLOP * 2
    ) {
      this._lastTap = null;
      if (this.zoomed) this.reset();
      else this._zoomAbout(point, TAP_SCALE);
      return;
    }
    this._lastTap = { ...point, at: now };
  }

  /**
   * Wheel zoom, for the desktop half of the audience.
   *
   * A trackpad pinch reaches the page as a wheel event with ctrl held, which is the same
   * shape as ctrl-scroll on a mouse — so both work, and a plain scroll is left alone.
   */
  _wheel(event) {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const point = this._local(event.clientX, event.clientY);
    this._zoomAbout(point, this._scale * Math.exp(-event.deltaY / 220));
  }
}
