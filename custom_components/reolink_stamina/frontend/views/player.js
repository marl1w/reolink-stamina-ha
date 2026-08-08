/**
 * Clip playback: the player element, and the decisions only it can make.
 *
 * What lives here is coordination — which recording, which resolution, where the playhead
 * is, and what the chrome says about all of it. The pieces it coordinates each have their
 * own module:
 *
 * * `playback/source.js` — getting the bytes into the video element, working down the ladder
 *   of routes when a browser cannot play what the recorder sends.
 * * `playback/routes.js` — that ladder, and the memory of which rung worked where.
 * * `playback/clip-window.js` — trimming a 24/7 segment down to its detections.
 * * `playback/save.js` — writing the clip on screen out as an MP4.
 * * `playback/zoom.js` — pinch, drag and double-tap to look closer at the picture.
 * * `playback/overlay.js` and `scrub-bar.js` — the two bits of chrome with behaviour.
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
 *    because wired cameras on a recorder generally do not expose their pre-alarm setting.
 *
 * Custom controls rather than the browser's, because a native control bar cannot be
 * overlaid with the trigger marker.
 */

import { adoptStyles, el, icon } from "../dom.js";
import { SHARED } from "../theme.js";
import { formatClock, formatSize, formatTime, sortTriggers, streamLabel, triggerMeta } from "../format.js";
import { clipWindow, clusterDetections } from "../playback/clip-window.js";
import { OVERLAY_STYLES, Overlay } from "../playback/overlay.js";
import { ROUTE_STREAM } from "../playback/routes.js";
import { SAVE_STYLES, assembleClip, clipFileName, saveBlob, saveMenuNodes } from "../playback/save.js";
import { PlaybackSource } from "../playback/source.js";
import { ZOOM_STYLES, PinchZoom } from "../playback/zoom.js";
import { SCRUB_STYLES, ScrubBar } from "./scrub-bar.js";

/**
 * How old the server's diagnosis may be and still be about the clip that just failed.
 *
 * The backend keeps the last failure however long ago it happened, and explaining this
 * clip with the previous one's reason would be worse than saying nothing. Generous enough
 * to cover a conversion that spent its whole thirty-second budget before giving up, plus
 * the ladder's own timeouts on top.
 */
const FAILURE_MAX_AGE_MS = 90_000;

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

/*
 * The stage is sized by the layout above it, never by what is in it.
 *
 * Everything it holds — the video, the overlay, the zoom pill — is positioned against it,
 * so its height is the one flex gives it and nothing inside can argue. That is not tidiness:
 * the video used to be a centred grid item at height 100%, which asks the row to size
 * itself from the video while the video sizes itself from the row. Chrome settles that
 * against the definite flex height; Safari settles it by feeding the video's aspect-derived
 * height back into the row, so widening the player past the picture's aspect grew the
 * element a little more each pass — the picture sinking below the stage with a growing band
 * of black above it, which is exactly what dragging the divider out did.
 *
 * Absolute rather than a grid item, so the video cannot contribute a height to anything and
 * the loop has no way to start, on any engine.
 */
.stage {
  position: relative;
  background: #000;
  flex: 1 1 auto;
  min-height: 200px;
}
video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  object-fit: contain;
  background: #000;
}

/* ---------------------------------------------------------------- controls */

.controls { padding: 10px 14px 12px; flex: 0 0 auto; display: flex; flex-direction: column; gap: 8px; }

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
.select[hidden] { display: none; }

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

.trim-btn { padding: 5px 10px; font-size: 0.78rem; white-space: nowrap; min-width: 0; }
.trim-btn[hidden] { display: none; }

/*
 * One line of controls on a phone.
 *
 * The row was wrapping onto a second line, which costs the picture about fifty pixels of a
 * screen that has none to spare. Everything that survives here is either a fixed-size button
 * or a short label — the resolution reads "Low" rather than "Low resolution", the trim button
 * keeps its length and drops its sentence — and the one item with no natural width, the
 * detection note, goes: the markers it describes are still on the bar above it, still with
 * their tooltips, and it is the only thing here that could push the row over on its own.
 */
@media (max-width: 620px) {
  .head { gap: 6px; padding: 10px 6px 10px 12px; }
  .head__sub { display: none; }
  .controls { padding: 6px 8px 10px; gap: 6px; }
  .buttons { flex-wrap: nowrap; gap: 2px; min-width: 0; }
  .times .sep { display: none; }
  .preroll-note { display: none; }
  .icon-btn { width: 36px; height: 36px; }
  .select { padding: 5px 6px; font-size: 0.78rem; }
  .trim-btn { padding: 5px 7px; font-size: 0.75rem; }
  .trim-btn__what { display: none; }

  /* Nothing wraps here any more, so the row has to survive a screen narrower than the sum
     of its parts. The buttons are the row's vocabulary and keep their size; the clock and
     the trim button are the two items that still read once shortened, so they are the two
     that give way — and the spacer between them goes first of all. */
  .buttons > .icon-btn,
  .buttons > .anchor,
  .buttons > .select { flex: 0 0 auto; }
  .times,
  .trim-btn { flex: 0 1 auto; min-width: 0; overflow: hidden; }
  .times { gap: 4px; }
}

/* A phone held sideways is short rather than narrow, and the stage's floor is what stops
   the header and the controls fitting alongside it. */
@media (max-height: 520px) {
  .stage { min-height: 120px; }
  .head { padding: 8px 10px; }
}
`;

/**
 * Element fullscreen, where the browser has it.
 *
 * Absent on an iPhone: iOS gives fullscreen to a `<video>` and its own native controls, and
 * to nothing else — there is no element fullscreen to ask for. So the button is hidden there
 * rather than left to fail quietly, and little is lost: the player already covers the whole
 * screen on a phone, which is most of what fullscreen was for.
 *
 * The prefixed names are Safari before 16.4, still current on plenty of Macs.
 */
const FULLSCREEN = (() => {
  const request = Element.prototype.requestFullscreen || Element.prototype.webkitRequestFullscreen;
  // The API also exists inside an iframe that was never granted the permission, where every
  // request rejects. `?? true` because a browser that does not report this simply allows it.
  const allowed = document.fullscreenEnabled ?? document.webkitFullscreenEnabled ?? true;
  if (!request || !allowed) return null;
  return {
    request,
    exit: document.exitFullscreen || document.webkitExitFullscreen,
    current: () => document.fullscreenElement || document.webkitFullscreenElement || null,
  };
})();

export class EventPlayer extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(
      this.shadowRoot,
      SHARED + STYLES + OVERLAY_STYLES + SCRUB_STYLES + SAVE_STYLES + ZOOM_STYLES
    );
    this._built = false;
    this._event = null;
    this._stream = null;
    /** The resolution the user last picked in the player, when they have picked one. */
    this._chosenStream = null;
    /**
     * Whether that choice was the user's own, rather than one the player made for them.
     *
     * The difference decides whether a resolution that fails may be swapped out silently.
     * Demoting the player's own guess is helpful; demoting a choice the user made by hand
     * reads as the player arguing with them, and since the choice is remembered they pick
     * it again and watch it happen again.
     */
    this._streamChosenByUser = false;
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
    /** The in-flight clip download, so it can be cancelled. */
    this._saving = null;

    this._onDocClick = (event) => {
      if (this._savePop?.hidden !== false) return;
      // Anywhere outside the menu closes it, including the video and the controls. Testing
      // against the whole player instead left it open while clicking around behind it.
      if (event.composedPath().includes(this._saveAnchor)) return;
      this._savePop.hidden = true;
    };
    this._onFullscreenChange = () => this._syncFullscreenButton();
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
    // Both names, because the event is prefixed on the same Safari versions the API is.
    document.addEventListener("fullscreenchange", this._onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", this._onFullscreenChange);
    this.addEventListener("keydown", this._onEscape);
  }

  disconnectedCallback() {
    document.removeEventListener("click", this._onDocClick);
    document.removeEventListener("fullscreenchange", this._onFullscreenChange);
    document.removeEventListener("webkitfullscreenchange", this._onFullscreenChange);
    this.removeEventListener("keydown", this._onEscape);
    this._saving?.abort();
    this._zoom?.detach();
    this._exitFullscreen();
    // Whatever the server was converting for us stops when we let go of the URL.
    this._source?.destroy();
  }

  set store(store) {
    this._store = store;
  }

  set api(api) {
    this._api = api;
    if (this._source) this._source.api = api;
  }

  /** Whether the beta that allows server-side conversion is switched on. */
  get _adaptive() {
    return Boolean(this._store?.options?.beta_restream);
  }

  /** Load an event, or clear the player when given null. */
  setEvent(event) {
    const sameEvent = this._event && event && this._event.id === event.id;
    this._event = event;
    if (!this._built) this._build();

    if (!event) {
      this._exitFullscreen();
      this._zoom.reset();
      this._source.destroy();
      this._video.removeAttribute("src");
      this._video.load();
      return;
    }

    if (!sameEvent) {
      // A zoom belongs to the picture it was made on, not to the player.
      this._zoom.reset();
      // Only when the clip really changed. This method is called again on every store
      // update — a search patch landing, detection counts arriving — and closing the menu
      // there made it vanish seemingly at random while a clip was playing.
      if (this._savePop) this._savePop.hidden = true;

      // Prefer the resolution the user last chose, when this clip has it.
      this._stream = this._preferredStream(event);
      this._detections = [];
      this._clip = null;
      this._untrimmed = false;
      this._atClipEnd = false;
      // Back to the start, now, rather than when the new clip first reports a time. That
      // report waits on the recorder answering and — on a converted route — on ffmpeg
      // producing something, so the bar and the clock would otherwise sit on the previous
      // clip's position for several seconds while a different clip was being opened.
      this._scrub.reset();
      this._current.textContent = formatClock(0);
      this._total.textContent = formatClock(0);
      this._source.reset({ event, stream: this._stream });
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
    this._source.open({ seek });
    this._renderChrome();
  }

  // ---------------------------------------------------------------------- clip

  /**
   * The stretch of a segment worth playing.
   *
   * Only for 24/7 footage. Where the camera records on events the recorder already cut
   * the clip to the event and wrote its pre-record buffer into the front of it, so
   * trimming again would cut into exactly the footage the panel exists to preserve.
   */
  _clipWindow(event, result) {
    if (!event.continuous) return null;
    return clipWindow({
      duration: this._windowDuration(),
      detections: this._detections,
      lead: result.clip_lead ?? 0,
      tail: result.clip_tail ?? 0,
    });
  }

  /**
   * The clip's bounds, or the whole recording when it is not trimmed.
   *
   * Asked rather than stored, because the bar, the markers and the stop at the end all read
   * it — and the user can put the whole segment back at any moment.
   */
  _clipBounds() {
    if (this._clip && !this._untrimmed) return this._clip;
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
    const position = this._source.displayTime;
    const { start, end } = this._clipBounds();
    if (this._atClipEnd || position < start || position >= end) {
      this._atClipEnd = false;
      // Expanding carries on from where the clip ran out; collapsing rewinds to the event.
      this._source.open({
        seek: Math.floor(this._untrimmed ? Math.max(position, this._clip.end) : start),
      });
    }
    this._onTime();
    this._renderChrome();
  }

  // ---------------------------------------------------------------- resolution

  /**
   * Low resolution unless the user has asked for something else.
   *
   * Low is the default for good reason: it is H.264 on every Reolink device seen so far and
   * starts far faster, while high is often H.265 at 4608x1728 — slow to open, slow to
   * decode, and undecodable outside Safari. With the adaptive beta on, high resolution can
   * be made to play anywhere, so the choice is offered again and remembered while the panel
   * is open; without it, low is all there is.
   */
  _preferredStream(event) {
    const streams = this._availableStreams(event);
    if (this._adaptive && this._chosenStream && streams.includes(this._chosenStream)) {
      return this._chosenStream;
    }
    if (streams.includes("sub")) return "sub";
    return streams[0] || "sub";
  }

  /** Every resolution this row exists in, whether or not it has been searched for. */
  _availableStreams(event) {
    const streams = [...(event?.streams || []), ...(event?.alternate_streams || [])];
    return streams.filter((stream, index) => streams.indexOf(stream) === index);
  }

  /**
   * Play this clip in another resolution.
   *
   * The ladder is reset rather than carried over: the resolutions are separate encodings,
   * and the low one is usually the H.264 that needs no conversion at all — so a clip
   * routed through ffmpeg in high resolution should not stay routed through it in low.
   */
  _switchStream(stream, { announce = "Switching resolution…", byUser = true } = {}) {
    if (!stream || stream === this._stream) return;
    const position = this._source.displayTime;
    this._chosenStream = stream;
    this._streamChosenByUser = byUser;
    this._stream = stream;
    this._atClipEnd = false;
    // The two resolutions are separate encodings and need not share an aspect ratio, so a
    // zoom carried across them would land somewhere other than where it was left.
    this._zoom.reset();
    // With the beta off, changing resolution must not also change route: the panel has
    // always simply reloaded whatever route was playing.
    this._source.reset({ event: this._event, stream, keepRoute: !this._adaptive });
    this._overlay.show("mdi:video-switch-outline", announce, { spinner: true });
    this._source.open({ seek: Math.floor(position), quiet: true });
    this._renderChrome();
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
    // A frame has been decoded, which is the only proof a route actually works — an
    // undecodable codec reaches "loadedmetadata" quite happily and then draws nothing.
    this._video.addEventListener("loadeddata", () => this._onDecoded());
    this._video.addEventListener("timeupdate", () => this._onTime());
    this._video.addEventListener("progress", () => this._onTime());
    this._video.addEventListener("play", () => this._renderPlayState());
    this._video.addEventListener("pause", () => this._renderPlayState());
    this._video.addEventListener("ended", () => this._renderPlayState());
    this._video.addEventListener("error", () => this._onError());
    // Mid-clip rebuffering: the recorder paces playback at about real time, so running dry
    // is normal and worth showing rather than freezing on a still frame.
    this._video.addEventListener("waiting", () => this._overlay.busy());
    this._video.addEventListener("playing", () => this._overlay.clearBusy());

    this._overlay = new Overlay({ onPlay: () => this._togglePlay() });
    this._stage = el("div", { class: "stage" }, this._video, this._overlay.element);
    // The gesture is measured against the stage and moves the video inside it. Its reset
    // pill goes in last, so it stays reachable while a message is on screen.
    this._zoom = new PinchZoom({ container: this._stage, target: this._video });
    this._stage.append(this._zoom.indicator);
    this._zoom.attach();

    this._source = new PlaybackSource({
      video: this._video,
      overlay: this._overlay,
      isAdaptive: () => this._adaptive,
      onRouteChange: () => this._renderChrome(),
      onGiveUp: (detail) => this._offerDownloadInstead(detail),
    });
    this._source.api = this._api;

    // --- scrub bar
    this._scrub = new ScrubBar({
      onSeek: (ratio) => this._seekToRatio(ratio),
      onTogglePlay: () => this._togglePlay(),
    });

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
      {
        class: "icon-btn",
        title: "Full screen",
        hidden: !FULLSCREEN,
        onclick: () => this._toggleFullscreen(),
      },
      (this._fullIcon = icon("mdi:fullscreen"))
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

    // Offered only with the adaptive beta on, because that is what makes high resolution
    // watchable outside Safari. Without it the panel plays low resolution and says so.
    this._qualitySelect = el("select", {
      class: "select quality",
      title: "Playback resolution",
      hidden: true,
      onchange: (changeEvent) => this._switchStream(changeEvent.target.value),
    });

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
      this._scrub.element,
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
        this._qualitySelect,
        this._trimBtn,
        this._saveAnchor,
        this._fullBtn
      )
    );

    this.shadowRoot.append(head, this._stage, this._controls);
    this._built = true;
  }

  /**
   * Nothing can play this clip here — so say so, and offer what does work.
   *
   * A download is not another attempt at the same thing: it is assembled in the browser from
   * the recorder's own bytes, trimmed to the clip, and handed to a player on the device that
   * can decode it. Better than the whole-file fallback this replaced, which was minutes long,
   * unseekable, and in the very codec that had just failed.
   */
  async _offerDownloadInstead(detail, lead = null) {
    if (!this._event) return;
    // The ladder gives up without knowing why: on a converted route the failure happened
    // on the server and arrived here as a numeric MediaError with the reason discarded.
    // Ask what the server made of it, and prefer its sentence — "this machine cannot
    // convert this fast enough" is worth saying, and nothing here could have worked it out.
    const diagnosed = lead ? null : await this._recentPlaybackFailure();
    if (!this._event) return; // navigated away while asking
    const other = this._availableStreams(this._event).find((stream) => stream !== this._stream);
    const opening =
      diagnosed?.message ||
      lead ||
      `This clip cannot be played in this browser${detail ? ` (${detail})` : ""}.`;
    this._overlay.show(
      "mdi:download-circle-outline",
      `${opening} Download it and it will play on your device${other ? `, or try ${streamLabel(other).toLowerCase()} resolution` : ""}.`,
      { action: { label: "Download this clip", onClick: () => this._saveClip(this._stream) } }
    );
  }

  /**
   * The resolution the user asked for could not be played — so say why, and offer the one
   * that can rather than substituting it behind their back.
   *
   * The reason comes from the server where there is one, because on a converted route this
   * is almost never "the browser refused it": it is a machine that could not re-encode a
   * full-sensor stream in time, or a recorder that would not send it fast enough. Neither
   * is guessable here, and both are worth saying — "switching to low resolution…" told the
   * user nothing about why high resolution keeps not working for them.
   */
  async _offerLowerResolution() {
    const diagnosed = await this._recentPlaybackFailure();
    if (!this._event) return;
    const reason =
      diagnosed?.message ||
      "High resolution could not be played here: it is H.265 at full sensor size, which this browser cannot decode and Home Assistant could not convert in time.";
    this._overlay.show(
      "mdi:quality-high",
      `${reason} Low resolution is H.264 and plays with no conversion at all.`,
      {
        action: {
          label: `Switch to ${streamLabel("sub").toLowerCase()} resolution`,
          onClick: () => this._switchStream("sub", { byUser: false }),
        },
      }
    );
  }

  /** The server's diagnosis, but only while it is still plausibly about this clip. */
  async _recentPlaybackFailure() {
    const failure = await this._api.playbackFailure();
    if (!failure?.message || !failure.at) return null;
    const age = Date.now() - Date.parse(failure.at);
    return Number.isFinite(age) && age >= 0 && age <= FAILURE_MAX_AGE_MS ? failure : null;
  }

  // ------------------------------------------------------------------ playback

  _togglePlay() {
    // The stream was dropped at the end of the clip, so there is nothing to resume:
    // pressing play means watch it again.
    if (this._atClipEnd) {
      this._atClipEnd = false;
      this._overlay.hide();
      this._source.open({ seek: Math.floor(this._clipBounds().start) });
      return;
    }
    if (this._video.paused) {
      // A route that has not drawn anything yet keeps its spinner: pressing play on a clip
      // still opening should not leave a black stage with nothing on it.
      if (this._source.confirmedDecoding) this._overlay.hide();
      else this._overlay.busy();
      this._video.play().catch(() => {});
    } else {
      this._video.pause();
    }
  }

  _toggleMute() {
    this._video.muted = !this._video.muted;
    this._renderPlayState();
  }

  /**
   * Fill the screen with the whole player, not only the picture.
   *
   * The stage alone used to be fullscreened, which is why fullscreen had no controls: they
   * are siblings of it, not children. Nor could the browser's own control bar stand in for
   * them — a native bar cannot carry the detection markers, which is why this panel draws its
   * own in the first place. So the element goes fullscreen, header and controls included.
   */
  _toggleFullscreen() {
    if (!FULLSCREEN) return;
    if (FULLSCREEN.current()) {
      this._exitFullscreen();
      return;
    }
    FULLSCREEN.request.call(this)?.catch?.(() => {});
  }

  /**
   * Come back out, whoever asked.
   *
   * Closing the player while it fills the screen has to, or the browser is left showing a
   * fullscreen element the panel has already hidden — which reads as a frozen black screen.
   */
  _exitFullscreen() {
    if (FULLSCREEN?.current()) FULLSCREEN.exit?.call(document)?.catch?.(() => {});
  }

  _syncFullscreenButton() {
    if (!this._built || !FULLSCREEN) return;
    const active = Boolean(FULLSCREEN.current());
    if (this._fullIcon.tagName.toLowerCase() === "ha-icon") {
      this._fullIcon.setAttribute("icon", active ? "mdi:fullscreen-exit" : "mdi:fullscreen");
    }
    this._fullBtn.title = active ? "Exit full screen" : "Full screen";
  }

  /**
   * The whole recording this row covers, which is what times are measured against.
   *
   * Never the element's own duration: it only ever holds the part it has been sent, and that
   * grows as the data arrives — which made the scrub bar start at a second and then stretch.
   * The recording's real length comes from the search result.
   */
  _windowDuration() {
    return this._event?.duration || 0;
  }

  /** What the scrub bar spans: the trimmed clip, or the whole recording. */
  _duration() {
    const { start, end } = this._clipBounds();
    return Math.max(0, end - start);
  }

  /**
   * The element knows something about the recording; nothing here depends on what.
   *
   * Seeking always works — every route reopens the recording at a new offset server-side —
   * so unlike the whole-file route this replaced, there is no seekability to establish.
   */
  _onMetadata() {
    // How far the picture may be panned depends on its shape, which is only knowable now.
    this._zoom.refresh();
    this._onTime();
    this._renderChrome();
  }

  /** A frame arrived, so the route works and the chrome may have something new to say. */
  _onDecoded() {
    this._source.noteDecoded();
    this._renderChrome();
  }

  _onTime() {
    const { start: clipStart } = this._clipBounds();
    const duration = this._duration();
    // Everything on the bar is relative to where the clip starts, not the recording.
    const shown = this._source.displayTime - clipStart;
    this._current.textContent = formatClock(Math.max(0, shown));
    this._total.textContent = formatClock(duration);

    const buffered = this._video.buffered;
    // Offset by where this stream began, or a seek would show the buffer back at zero.
    const reached =
      buffered.length > 0
        ? this._source.timeOffset + buffered.end(buffered.length - 1) - clipStart
        : null;
    this._scrub.update({ position: shown, duration, buffered: reached });

    if (shown >= duration && duration > 0) this._onReachedEnd();
  }

  /**
   * A trimmed clip has to be stopped by us; the recorder would keep sending the rest of
   * the segment quite happily, and a 20-second event would run on for minutes.
   *
   * The stream is dropped rather than merely paused, because pausing the element does not
   * stop the demuxer pulling from the recorder — nor the server converting for it.
   */
  _onReachedEnd() {
    if (this._atClipEnd || !this._clip || this._untrimmed) return;
    this._atClipEnd = true;
    this._video.pause();
    this._source.destroy();
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

  /**
   * What to do when the route ladder has run out.
   *
   * Everything up to here is the source's business. What is left is a choice only the
   * player can make: low resolution is H.264 on every recorder seen so far, so it is worth
   * one attempt before declaring the clip unwatchable.
   */
  _onError() {
    const { handled, decodeFailure } = this._source.handleError();
    if (handled) return;

    const streams = this._availableStreams(this._event);
    if (decodeFailure && this._stream !== "sub" && streams.includes("sub")) {
      // A resolution the player chose itself may be swapped out for one that works. A
      // resolution the user chose by hand may not: doing it silently is what makes the
      // player look like it is flipping between the two of its own accord, and the choice
      // being remembered means the next clip does it all over again.
      if (this._streamChosenByUser) {
        this._offerLowerResolution();
        return;
      }
      this._switchStream("sub", {
        byUser: false,
        announce:
          "High resolution could not be decoded — it is H.265 at full sensor size. Switching to low resolution…",
      });
      return;
    }

    // The one case worth its own wording, because there is something the user can do about
    // it that is better than downloading every clip.
    if (decodeFailure && !this._adaptive) {
      this._offerDownloadInstead(
        null,
        "This browser cannot decode this recording — Reolink encodes it as H.265, which most browsers refuse. Turning on “Adaptive playback” in this integration's options has Home Assistant convert it instead."
      );
      return;
    }

    // Name what failed. An identical message for every different cause made separate faults
    // look like one unfixed bug.
    const detail = [
      `route: ${this._source.route}`,
      `quality: ${streamLabel(this._stream)}`,
      this._video.error ? `media error ${this._video.error.code}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    this._offerDownloadInstead(detail);
  }

  // ---------------------------------------------------------------- scrubbing

  /**
   * Seeking is asking the recorder to reopen the recording somewhere else.
   *
   * Whichever route is playing does that the same way, so a seek is not the moment to
   * reconsider which route it is.
   */
  _seekToRatio(ratio) {
    const duration = this._duration();
    if (duration <= 0) return;
    // The bar spans the clip, so a ratio along it lands at an offset into the recording.
    const { start: clipStart } = this._clipBounds();
    const target = clipStart + Math.max(0, Math.min(duration - 1, ratio * duration));
    // Seeking backwards out of a finished clip is how you rewatch it.
    this._atClipEnd = false;
    this._overlay.show("mdi:progress-clock", "Seeking…", { spinner: true });
    this._source.open({ seek: target, quiet: true });
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

  _renderSaveMenu() {
    const event = this._event;
    if (!event) return;
    this._savePop.replaceChildren(
      ...saveMenuNodes({
        streams: this._availableStreams(event),
        length: this._duration(),
        onChoose: (stream) => {
          this._savePop.hidden = true;
          this._saveClip(stream);
        },
      })
    );
  }

  /**
   * Save the clip on screen — trimmed if the bar is trimmed, the whole segment if not.
   *
   * Always the recorder's original footage, whatever route the player itself had to take
   * to show it: the bytes are read from the pass-through stream and put into an MP4 here.
   */
  async _saveClip(stream) {
    const event = this._event;
    if (!event || this._saving) return;

    this._video.pause();
    this._saving = new AbortController();
    this._overlay.show("mdi:tray-arrow-down", "Assembling clip… 0%", {
      spinner: true,
      cancel: () => this._saving?.abort(),
    });

    try {
      const blob = await assembleClip({
        api: this._api,
        event,
        stream,
        bounds: this._clipBounds(),
        signal: this._saving.signal,
        onProgress: (share) => this._overlay.setText(`Assembling clip… ${share}%`),
      });
      if (this._event?.id !== event.id) return; // moved on while it was being written
      saveBlob(blob, clipFileName(event, stream));
      this._overlay.hide();
    } catch (error) {
      if (error?.name === "AbortError") {
        this._overlay.hide();
      } else {
        this._overlay.show(
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
    this._trimBtn.hidden = !this._trimmable;
    if (this._trimBtn.hidden) return;

    const whole = this._windowDuration();
    const event = Math.max(0, this._clip.end - this._clip.start);
    // Two spans rather than one sentence: on a phone the wording is dropped and the length
    // kept, because the length against the one on the clock is the whole point of the button.
    this._trimBtn.replaceChildren(
      icon(this._untrimmed ? "mdi:arrow-collapse-horizontal" : "mdi:arrow-expand-horizontal"),
      el("span", {
        class: "trim-btn__what",
        text: this._untrimmed ? "Event only" : "Full segment",
      }),
      el("span", { class: "tabular", text: formatClock(this._untrimmed ? event : whole) })
    );
    this._trimBtn.title = this._untrimmed
      ? "Trim back to the detections in this segment"
      : "Play the whole recording segment, not just the event";
  }

  /**
   * Offer the resolutions this clip exists in.
   *
   * Only with the adaptive beta on: without it, high resolution is H.265 that most browsers
   * will not decode, and offering it was mostly a way to make playback worse.
   */
  _renderQuality() {
    const streams = this._adaptive ? this._availableStreams(this._event) : [];
    this._qualitySelect.hidden = streams.length < 2;
    if (this._qualitySelect.hidden) return;

    const wanted = streams.join("|");
    if (this._qualitySelect.dataset.built !== wanted) {
      // "Low", not "Low resolution": the word is what tipped the control row onto a second
      // line on a phone, and the select's own label already says what it picks.
      this._qualitySelect.replaceChildren(
        ...streams.map((stream) => el("option", { value: stream, text: streamLabel(stream) }))
      );
      this._qualitySelect.dataset.built = wanted;
    }
    this._qualitySelect.value = this._stream;
  }

  /**
   * Say how this clip is reaching the browser.
   *
   * Worth stating rather than hiding: conversion costs the machine something, it caps the
   * picture at 1080p, and someone reporting "playback is slow" needs to know which of the
   * routes they are on. Direct play is stated for the same reason from the other side — it
   * is the good case, and naming it is what makes the other two read as the exception.
   *
   * Only once a frame has actually been decoded, though. Until then the pass-through route
   * has proved nothing, and a badge that claims direct play and then changes its mind to
   * "re-encoded" is worse than one that waited a second.
   */
  _routeChip() {
    const label = this._source.routeLabel;
    if (!label) return null;
    if (this._source.route === ROUTE_STREAM && !this._source.confirmedDecoding) return null;
    const chip = el(
      "span",
      { class: "chip", dataset: { tone: "neutral" } },
      icon(label.icon),
      label.badge
    );
    chip.title = label.hint;
    return chip;
  }

  /**
   * Place the detection markers, or — failing those — the estimated trigger.
   *
   * Real detection times beat an estimated pre-roll, so exact marks are used wherever
   * Home Assistant's own history could supply them.
   */
  _renderMarkers(triggers) {
    const duration = this._duration();

    if (this._detections.length > 0 && duration > 0) {
      const { start: clipStart, end: clipEnd } = this._clipBounds();
      // Anything still detecting when the clip opens is marked at its start rather than
      // dropped: an event that began just before the segment boundary is the whole reason
      // this segment is tagged, and it is on screen from the first frame.
      const marks = this._detections
        .filter((d) => (d.end_offset ?? d.offset) > clipStart && d.offset < clipEnd)
        .map((d) => ({ ...d, offset: Math.max(d.offset, clipStart) }));
      if (marks.length > 0) {
        // When the model marked something inside this recording, the moments it marked, as
        // offsets into the same clip — so a cluster can say whether it is one of them.
        const started = Date.parse(this._event.start);
        const odd = (this._store?.eventRelevance(this._event) || [])
          .filter((item) => item.unusual)
          .map((item) => (Date.parse(item.at) - started) / 1000);

        this._scrub.setMarkers(
          clusterDetections(marks, duration).map((cluster) => {
            // Coloured by the most significant thing that fired here, which is the same
            // rule the row's leading icon uses: a car arriving is a vehicle, not motion.
            const meta = triggerMeta(sortTriggers(cluster.kinds)[0]);
            // Within a few seconds is the same detection: the recorder's offsets and Home
            // Assistant's timestamps agree to about that, and no two clusters are that close.
            const unusual = odd.some((at) => Math.abs(at - cluster.offset) <= 5);
            return {
              ratio: (cluster.offset - clipStart) / duration,
              tone: meta.tone,
              exact: true,
              unusual,
              title:
                `${cluster.kinds.map((kind) => triggerMeta(kind).label).join(", ")} detected ` +
                `${Math.round(cluster.offset)}s into the recording` +
                (unusual ? " — unusual for this camera" : ""),
            };
          })
        );
        const overall = triggerMeta(sortTriggers(marks.map((d) => d.kind))[0]);
        this._prerollNote.replaceChildren(
          el("span", { class: "swatch", dataset: { exact: "true", tone: overall.tone } }),
          el("span", {
            text: marks.length > 1 ? `${marks.length} detections` : `${overall.label} detected`,
          })
        );
        return;
      }
    }

    const preRoll = this._event.pre_roll || { seconds: 0, exact: false };
    if (duration > 0 && preRoll.seconds > 0 && preRoll.seconds < duration) {
      // No detection to attribute this to, so it takes the tone of whatever the recorder
      // tagged the recording with.
      this._scrub.setMarkers([
        {
          ratio: preRoll.seconds / duration,
          tone: triggers.length ? triggerMeta(triggers[0]).tone : "neutral",
          exact: Boolean(preRoll.exact),
          title: preRoll.exact
            ? `Trigger fired ${preRoll.seconds}s in (pre-record buffer before this point)`
            : `Trigger fired around ${preRoll.seconds}s in (estimated — this camera does not report its pre-record time)`,
        },
      ]);
      this._prerollNote.replaceChildren(
        el("span", { class: "swatch", dataset: { exact: String(Boolean(preRoll.exact)) } }),
        el("span", { text: preRoll.exact ? "trigger" : "trigger (est.)" })
      );
      return;
    }

    this._scrub.setMarkers([]);
    this._prerollNote.replaceChildren();
  }

  _renderChrome() {
    const event = this._event;
    if (!event) return;

    this._renderTrimButton();
    this._renderQuality();
    // Nothing to write a file from when the recorder listed no resolution for this row.
    this._saveBtn.hidden = !(event.streams?.length || event.alternate_streams?.length);
    // The menu quotes the clip's length and how long saving it will take, so an open one is
    // rebuilt whenever anything it quotes may have moved under it.
    if (this._savePop.hidden === false) this._renderSaveMenu();
    this._title.textContent = event.camera;
    this._sub.textContent = `${event.device} · ${formatTime(event.start)} · ${formatSize(event.size)}`;

    const triggers = sortTriggers(event.triggers || []);
    this._chips.replaceChildren(
      ...(triggers.length
        ? triggers.map((trigger) => {
            const meta = triggerMeta(trigger);
            return el("span", { class: "chip", dataset: { tone: meta.tone } }, icon(meta.icon), meta.label);
          })
        : [el("span", { class: "chip", dataset: { tone: "neutral" } }, "Recording")]),
      ...[this._routeChip()].filter(Boolean)
    );

    this._renderMarkers(triggers);

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
