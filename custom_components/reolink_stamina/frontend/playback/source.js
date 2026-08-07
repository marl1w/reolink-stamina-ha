/**
 * Getting a recording into the video element, whatever it takes.
 *
 * This owns the ladder described in `routes.js`: it opens the cheapest route, notices when
 * that route has failed, and works down to the next. Nothing above it needs to know which
 * rung is playing — only the chrome does, and that is told rather than asking.
 *
 * Everything it opens is addressed by an *absolute* offset into the recording, in seconds,
 * and every route seeks the same way: server-side, by reopening the recording somewhere
 * else. That is what makes footage nothing can seek by byte scrubbable at all. The element
 * therefore always plays from zero, and `timeOffset` is added back for display.
 *
 * When every route has failed there is nothing further to try. The recorder's own whole-file
 * download used to sit at the bottom of the ladder and was never worth having: unseekable,
 * minutes long, and carrying the very codec the browser had just refused. The player says so
 * and offers the download instead, which is a different thing entirely — assembled in the
 * browser, trimmed to the clip, and played locally by something that can.
 */

import { canDemux, decodedFrames, isHevcCodec, loadDemuxer } from "./demuxer.js";
import {
  CONVERTED_FORMAT,
  CONVERTED_ROUTES,
  HEVC_SUPPORTED,
  ROUTE_LABELS,
  ROUTE_STREAM,
  ROUTE_TRANSCODE,
  nextRoute,
  readRouteMemory,
  recalledRoute,
  rememberRoute,
  routeMemoryKeys,
  writeRouteMemory,
} from "./routes.js";

/** How often to ask whether anything is actually being decoded. */
const DECODE_POLL_MS = 500;
/**
 * How long a route may hold data it is not decoding before it counts as failed.
 *
 * Short, because by this point the bytes have arrived and a working decoder would already
 * have drawn something.
 */
const DECODE_STALL_MS = 4000;
/**
 * How long a route may produce nothing at all, data included.
 *
 * Generous: a recorder takes a second or two to answer, and an HLS session has to write two
 * segments before a playlist is worth handing to a player.
 */
const DECODE_CEILING_MS = 25000;
/**
 * The same, for a route Home Assistant is converting.
 *
 * Longer than the others on purpose, and longer than the server's own `FIRST_OUTPUT_TIMEOUT`
 * of thirty seconds. A converted route has a server-side startup cost the passthrough route
 * does not: the recorder is asked to seek, ffmpeg opens the input, and on a re-encode four
 * seconds of video have to be produced before there is a playlist worth handing over. Giving
 * up before the server has — which is what a twenty-five second ceiling did — abandons
 * conversions that were seconds from working, and on high resolution that is most of them.
 * The visible symptom was a player that dropped back to low resolution for no stated reason.
 */
const CONVERTED_CEILING_MS = 40000;

export class PlaybackSource {
  /**
   * `isAdaptive` is asked rather than passed, because the option can change under an open
   * panel. `onRouteChange` is how the chrome learns it has something new to say, and
   * `onGiveUp` is called when the ladder is exhausted — what to do about that is the
   * player's business, not this one's.
   */
  constructor({ video, overlay, isAdaptive, onRouteChange, onGiveUp }) {
    this._video = video;
    this._overlay = overlay;
    this._isAdaptive = isAdaptive;
    this._onRouteChange = onRouteChange;
    this._onGiveUp = onGiveUp;

    this._api = null;
    this._event = null;
    this._stream = null;
    this._route = ROUTE_STREAM;
    /** The route that set the element's current src, so it can be torn down correctly. */
    this._srcRoute = null;
    /** Seconds into the recording that the current stream starts at. */
    this._timeOffset = 0;
    /** Set once this clip has proved the browser cannot decode its codec. */
    this._codecFailure = false;
    /**
     * Whether the recording is H.265, where the demuxer got far enough to say.
     *
     * Unknown on a route where nothing demuxes it here — an iPhone, or either conversion —
     * and that matters: it is what tells a stalled decoder apart from a container this
     * browser could not read, which want opposite answers.
     */
    this._sourceIsHevc = false;
    /** Set once a frame has been decoded on the current route. */
    this._decoding = false;
    this._decodeProbe = null;
    this._player = null;
  }

  set api(api) {
    this._api = api;
  }

  get route() {
    return this._route;
  }

  get routeLabel() {
    return ROUTE_LABELS[this._route] || null;
  }

  /** Whether a frame has actually been decoded on the current route. */
  get confirmedDecoding() {
    return this._decoding;
  }

  /** Seconds into the recording that the current stream begins at. */
  get timeOffset() {
    return this._timeOffset;
  }

  /** Seconds into the recording currently shown, across a time-shifted stream. */
  get displayTime() {
    return this._timeOffset + (this._video.currentTime || 0);
  }

  /**
   * Point at a row and a resolution, and choose where to start from.
   *
   * The route is whatever worked for this camera, this resolution and this browser last
   * time, so the ladder is walked once and not on every clip. Passthrough otherwise, which
   * is what an installation with the beta off always gets — and `keepRoute` is how such an
   * install changes resolution without the route silently changing under it too.
   */
  reset({ event, stream, keepRoute = false }) {
    this._event = event;
    this._stream = stream;
    this._codecFailure = false;
    this._sourceIsHevc = false;
    this._decoding = false;
    this._timeOffset = 0;
    if (!keepRoute) this._route = this._recalledRoute(event, stream);
    return this._route;
  }

  _recalledRoute(event, stream) {
    if (!this._isAdaptive()) return ROUTE_STREAM;
    const recalled = recalledRoute(readRouteMemory(), routeMemoryKeys(event, stream));
    // Only a conversion is worth recalling; anything else means "start from the top".
    return CONVERTED_ROUTES.has(recalled) ? recalled : ROUTE_STREAM;
  }

  /** Note that this route works here, so the next clip opens straight onto it. */
  _rememberWorkingRoute() {
    if (!this._isAdaptive() || !this._event) return;
    const memory = readRouteMemory();
    const keys = routeMemoryKeys(this._event, this._stream);
    if (recalledRoute(memory, keys) === this._route) return;
    writeRouteMemory(rememberRoute(memory, keys, this._route));
  }

  // ------------------------------------------------------------------- opening

  /**
   * Open the recording at `seek` seconds in.
   *
   * `quiet` is for a caller that has already said what it is doing — changing route,
   * changing resolution, seeking — so its wording is not immediately overwritten.
   */
  async open({ seek = 0, quiet = false } = {}) {
    const event = this._event;
    if (!event) return;
    // Any previous demuxer or converted stream must go before another source is attached.
    this.destroy();
    const file = this.fileFor(this._stream);

    if (!quiet) {
      this._overlay.show("mdi:progress-download", "Opening recording…", { spinner: true });
    }

    if (this._route === ROUTE_STREAM) await this._openPassthrough(file, seek);
    else await this._openConverted(file, seek);
  }

  /**
   * What identifies this row's recording in a given resolution.
   *
   * Empty for a resolution the panel never searched: the backend then resolves it from the
   * event's time window instead, which is why browsing does not pay for every resolution.
   */
  fileFor(stream) {
    return this._event?.files?.[stream] || { name: "", start_id: "", playback_id: "", offset: 0 };
  }

  /** The arguments every route shares: which recording, and where in it to begin. */
  _streamArgs(file, seek) {
    return {
      entryId: this._event.entry_id,
      channel: this._event.channel,
      stream: this._stream,
      filename: file.name,
      // The recording's start, not this row's: a long recording holds several rows.
      startId: file.file_start_id || file.start_id,
      playbackId: file.playback_id,
      offset: file.offset || 0,
      start: this._event.start,
      end: this._event.end,
      seek,
    };
  }

  /**
   * The recorder's own FLV, demuxed here.
   *
   * No server work at all, which is why it is tried first and why this integration needs
   * no ffmpeg for playback on a desktop browser.
   */
  async _openPassthrough(file, seek) {
    const event = this._event;
    let demuxer;
    try {
      demuxer = await loadDemuxer();
    } catch {
      demuxer = null;
    }

    if (!canDemux(demuxer)) {
      // No Media Source Extensions, which is every iPhone: nothing here can demux the
      // recorder's FLV at all. With the beta on the server repackages it instead — the route
      // that makes the Home Assistant app on an iPhone work. Explicitly not a decode
      // failure: the browser could not *read* the container and its own decoder was never
      // asked for an opinion, so repackaging is exactly the fix.
      this._giveUpUnless(this.failover("this browser has no Media Source Extensions", false));
      return;
    }

    try {
      const { url } = await this._api.streamUrl(this._streamArgs(file, seek));
      if (this._event?.id !== event.id) return; // switched away while opening

      this.destroy();
      this._begin(ROUTE_STREAM, seek);

      // Live-paced rather than a seekable file: the recorder sends at roughly real time
      // and reports no length, so the demuxer must not try to seek within it.
      const player = demuxer.createPlayer(
        { type: "flv", isLive: true, url },
        { enableStashBuffer: false, stashInitialSize: 128, lazyLoad: false }
      );
      this._player = player;
      player.attachMediaElement(this._video);
      player.on(demuxer.Events.ERROR, () => this.handleError());
      // The demuxer names the codec as soon as it has read the stream's header, which is
      // the earliest anything can know the browser will refuse it. Acting on that saves
      // several seconds of black window before the watchdog would have noticed.
      player.on(demuxer.Events.MEDIA_INFO, (info) => this.handleMediaInfo(info));
      player.load();
      // The overlay stays up until a frame actually arrives — see `_checkDecoding`. On a
      // slow recorder, or a route that is about to fail, that is all the user has to go on.
      this._armDecodeProbe();
      this._attemptPlay(player.play());
    } catch (err) {
      if (this._event?.id !== event.id) return;
      // eslint-disable-next-line no-console
      console.warn("Reolink Stamina: stream unavailable", err);
      this._giveUpUnless(this.failover("the stream could not be opened"));
    }
  }

  /**
   * A recording Home Assistant is converting for us — repackaged, or re-encoded.
   *
   * Seeks the same way as the passthrough route, so everything the scrub bar does still
   * applies; only the bytes take a different path to get here.
   */
  async _openConverted(file, seek) {
    const event = this._event;
    try {
      const { url } = await this._api.streamUrl({
        ...this._streamArgs(file, seek),
        route: this._route,
        format: CONVERTED_FORMAT,
      });
      if (this._event?.id !== event.id) return; // switched away while opening

      this._begin(this._route, seek);
      this._video.src = url;
      this._video.load();
      this._armDecodeProbe();
      this._attemptPlay(this._video.play());
    } catch (err) {
      if (this._event?.id !== event.id) return;
      // eslint-disable-next-line no-console
      console.warn("Reolink Stamina: conversion unavailable", err);
      this._giveUpUnless(
        this.failover(err?.message || "the conversion could not be started"),
        err?.message
      );
    }
  }

  /** Common bookkeeping for a route that has just been handed a URL. */
  _begin(route, seek) {
    this._timeOffset = Math.max(0, Math.floor(seek));
    this._srcRoute = route;
  }

  _attemptPlay(attempt) {
    if (attempt?.catch) {
      // Autoplay blocked: offer an explicit play button instead of failing.
      attempt.catch(() => this._overlay.show(null, "", { play: true }));
    }
  }

  // ------------------------------------------------------------------ failures

  /**
   * Move to another route, keeping the place in the recording.
   *
   * Every route reopens the recording server-side, so a route change is simply the same
   * offset asked for a different way.
   */
  enterRoute(route) {
    const resumeAt = this.displayTime;
    this._route = route;
    this._onRouteChange();
    this._overlay.show(
      ROUTE_LABELS[route]?.icon || "mdi:progress-clock",
      route === ROUTE_TRANSCODE
        ? "This browser cannot play this recording, so Home Assistant is converting it…"
        : "This browser cannot play the recorder's own stream, so Home Assistant is repackaging it…",
      { spinner: true }
    );
    this.open({ seek: resumeAt, quiet: true });
  }

  /**
   * Give up on the current route and try the next one down the ladder.
   *
   * Returns false when there was nothing left to try, which is the caller's cue to explain
   * the failure rather than keep spinning.
   */
  failover(reason, decodeFailure = false) {
    const next = nextRoute(this._route, { adaptive: this._isAdaptive(), decodeFailure });
    if (!next) return false;
    // eslint-disable-next-line no-console
    console.debug(`Reolink Stamina: ${this._route} route failed (${reason}); trying ${next}`);
    this.enterRoute(next);
    return true;
  }

  /**
   * Say that this clip cannot be played here, and offer the thing that does work.
   *
   * The end of the ladder, and deliberately not another attempt: a download is assembled in
   * the browser from the recorder's own bytes and played by something that can decode them,
   * which is a better answer than a minutes-long file in the codec that just failed.
   */
  _giveUpUnless(handled, detail = null) {
    if (!handled) this._onGiveUp(detail);
  }

  /**
   * Handle an error from the element or the demuxer.
   *
   * Returns whether it was dealt with here. When it was not, the ladder is exhausted and
   * only the player can decide what to say — or whether the other resolution is worth a try.
   */
  handleError() {
    if (!this._video.getAttribute("src")) return { handled: true, decodeFailure: false };

    // A decode failure means the bytes arrived fine and the codec is the problem, so
    // repackaging would change nothing: what has to change is the codec.
    const mediaError = this._video.error;
    const decodeFailure = Boolean(mediaError && (mediaError.code === 3 || mediaError.code === 4));
    if (decodeFailure) this._codecFailure = true;

    // The stream may fail well after the URL was handed over, since the recorder only
    // answers once something reads it.
    const moved = this.failover(
      decodeFailure ? "the codec was refused" : "the route failed",
      decodeFailure
    );
    return { handled: moved, decodeFailure };
  }

  /**
   * Act on what the demuxer says the stream contains.
   *
   * Only one thing here is decided in advance rather than on failure, and only because the
   * browser itself answers it: whether it can decode HEVC at all. Chrome and Firefox
   * cannot, and their Media Source Extensions accept the stream anyway and then draw
   * nothing — the black window this beta exists for.
   */
  handleMediaInfo(info) {
    this._sourceIsHevc = isHevcCodec(info?.videoCodec);
    if (!this._sourceIsHevc || HEVC_SUPPORTED || !this._isAdaptive()) return;
    this._codecFailure = true;
    this.failover("this browser cannot decode H.265", true);
  }

  /**
   * Watch for a route that loads, reports no error, and never draws anything.
   *
   * This is the black window users report, and it has two shapes. Chrome and Firefox accept
   * an H.265 stream into Media Source Extensions and quietly decline to decode it; Safari
   * goes further and reports a size, a duration and `readyState` for a stream its decoder
   * then stalls on. Neither fires an error, and both look healthy from every angle except
   * one — no frame ever arrives.
   *
   * So this polls the frame counter rather than trusting a single deadline, and separates
   * "still loading" from "loaded and not decoding": an HLS session legitimately takes
   * several seconds to produce its first segments, while a stalled decoder sits on data it
   * is doing nothing with.
   */
  _armDecodeProbe() {
    this._clearDecodeProbe();
    // Runs whether or not the beta is on: with it off nothing fails over, but this is still
    // what tells the loading spinner that a picture has arrived.
    this._decoding = false;
    this._probeStartedAt = Date.now();
    this._dataSince = null;
    this._decodeProbe = setInterval(() => this._checkDecoding(), DECODE_POLL_MS);
  }

  _clearDecodeProbe() {
    if (this._decodeProbe !== null) clearInterval(this._decodeProbe);
    this._decodeProbe = null;
  }

  /**
   * Decide whether this route is drawing, still trying, or done for.
   *
   * `decodedFrames` returns null on a browser that will not say, and there the old signals
   * are all there is: a size, some data, and a clock that has moved.
   */
  _checkDecoding() {
    const video = this._video;
    if (!this._event) {
      this._clearDecodeProbe();
      return;
    }

    const frames = decodedFrames(video);
    const hasData = video.readyState >= 2;
    const decoding =
      frames === null ? hasData && video.videoWidth > 0 && video.currentTime > 0 : frames > 0;
    if (decoding) {
      this._clearDecodeProbe();
      this._decoding = true;
      // Only now is there something to look at, so only now does the spinner come down.
      this._overlay.hide();
      this._rememberWorkingRoute();
      // And only now is the route worth claiming: "playing straight from the recorder" is a
      // different statement from "opened and waiting to see", and the badge makes it.
      this._onRouteChange();
      return;
    }

    // Nothing is expected of a video nobody has pressed play on: autoplay was blocked and
    // the overlay is asking. Time spent paused is not time spent failing, so both clocks
    // are pushed along with it.
    if (video.paused) {
      this._probeStartedAt = Date.now();
      this._dataSince = null;
      return;
    }

    if (hasData && this._dataSince === null) this._dataSince = Date.now();
    const stalled = this._dataSince !== null && Date.now() - this._dataSince > DECODE_STALL_MS;
    const ceiling = CONVERTED_ROUTES.has(this._srcRoute) ? CONVERTED_CEILING_MS : DECODE_CEILING_MS;
    const silent = Date.now() - this._probeStartedAt > ceiling;
    if (!stalled && !silent) return;

    this._clearDecodeProbe();

    // With the beta off there is no ladder to walk, and nothing this can convert: the
    // player says so and offers the download.
    if (!this._isAdaptive()) {
      this._onGiveUp(null);
      return;
    }

    // A stream that arrived and would not decode is the decoder's failure, not the
    // container's — but only where we know the codec, since repackaging is the right answer
    // to an H.264 stream this browser could not read.
    const blame = stalled && this._sourceIsHevc;
    this._giveUpUnless(this.failover(stalled ? "nothing was decoded" : "nothing arrived", blame));
  }

  /** The element says it has data; check straight away rather than waiting for the poll. */
  noteDecoded() {
    if (this._decodeProbe !== null) this._checkDecoding();
  }

  /**
   * Tear down whatever is playing, so an abandoned stream stops being pulled.
   *
   * The converted routes matter most here: each is a live HTTP response with ffmpeg on the
   * other end, and the server only stops converting when the element lets go of the URL.
   * Pausing does not do it.
   */
  destroy() {
    this._clearDecodeProbe();

    const player = this._player;
    this._player = null;
    if (player) {
      try {
        player.pause();
        player.unload();
        player.detachMediaElement();
        player.destroy();
      } catch {
        // Already gone.
      }
    }

    if (this._srcRoute !== null) {
      this._srcRoute = null;
      this._video.removeAttribute("src");
      this._video.load();
    }
  }
}
