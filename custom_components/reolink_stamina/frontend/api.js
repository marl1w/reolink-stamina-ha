/** Thin wrapper over Home Assistant's websocket connection. */

const DOMAIN = "reolink_stamina";

export class StaminaApi {
  constructor(hass) {
    this.hass = hass;
  }

  /** Discovered devices plus the integration's options. Cheap; no round trip to any of them. */
  async devices() {
    return this.hass.callWS({ type: `${DOMAIN}/devices` });
  }

  /**
   * Subscribe to event rows. The first message is a snapshot built from cache and
   * arrives immediately; `patch` messages follow as each device answers.
   */
  subscribeEvents({ targets, startDate, endDate, force = false }, callback) {
    return this.hass.connection.subscribeMessage(callback, {
      type: `${DOMAIN}/events`,
      targets,
      start_date: startDate,
      end_date: endDate,
      force,
    });
  }

  /** Subscribe to which days of a month hold recordings. */
  subscribeCalendar({ targets, year, month, force = false }, callback) {
    return this.hass.connection.subscribeMessage(callback, {
      type: `${DOMAIN}/calendar`,
      targets,
      year,
      month,
      force,
    });
  }

  /**
   * Sign a path so a <video> element can fetch it.
   *
   * The Reolink video proxy requires authentication, and a media element cannot send
   * an Authorization header — so Home Assistant's own signing command is used, exactly
   * as the built-in media browser does.
   */
  async signPath(path, expires = 3600) {
    const result = await this.hass.callWS({ type: "auth/sign_path", path, expires });
    return result.path;
  }

  /**
   * The exact moments detections fired inside a recording.
   *
   * A device only tags a whole segment, so this comes from Home Assistant's recorder.
   * It is what lets playback open just before the event rather than at the start of a
   * long segment.
   */
  /**
   * What "learn what is normal" makes of one camera over a window.
   *
   * Answers while a camera is still collecting, and that is deliberate: the scores mean
   * nothing yet, but what has been *collected* about each detection does, and showing it is
   * what makes it worth having before it can say anything.
   */
  async relevance({ entryId, channel, start, end }) {
    return this.hass.callWS({
      type: `${DOMAIN}/relevance`,
      entry_id: entryId,
      channel,
      start,
      end,
    });
  }

  /**
   * What one camera has learned, as distributions rather than as a verdict on one event.
   *
   * The counterpart of `relevance` above, and arguably the more useful half: that one says
   * why an event stood out, this one says what it stood out from.
   */
  async relevanceProfile(targets) {
    return this.hass.callWS({
      type: `${DOMAIN}/relevance_profile`,
      targets: targets.map(({ entryId, channel }) => ({ entry_id: entryId, channel })),
    });
  }

  async detections({ entryId, channel, start, end }) {
    return this.hass.callWS({
      type: `${DOMAIN}/detections`,
      entry_id: entryId,
      channel,
      start,
      end,
    });
  }

  /**
   * A signed URL that streams a recording, optionally starting `seek` seconds in.
   *
   * By default the recorder serves FLV and the browser demuxes it itself — the same thing
   * the recorder's own web player does. Home Assistant only passes the bytes through, so
   * nothing is transcoded and there is no server-side process to outlive the clip.
   *
   * `route` asks for one of the conversion routes instead, for a browser that cannot
   * play what the recorder sends: `remux` repackages, `transcode` re-encodes. `format`
   * says which container this browser can take — HLS for anything Apple.
   *
   * Seeking is server-side whichever route is used: a different offset means a different
   * stream.
   */
  async streamUrl({
    entryId,
    channel,
    stream,
    filename = "",
    startId = "",
    playbackId = "",
    // Which device answered for this row, when that is not the camera it is filed under.
    // The backend checks it against the camera's pairing rather than taking it on trust.
    sourceEntryId = null,
    sourceChannel = null,
    offset = 0,
    start = "",
    end = "",
    seek = 0,
    route = "passthrough",
    format = "mp4",
  }) {
    const result = await this.hass.callWS({
      type: `${DOMAIN}/stream_url`,
      entry_id: entryId,
      channel,
      stream,
      // Empty when this resolution was never searched; the backend resolves it from the
      // time window instead, which is why browsing does not pay for every resolution.
      filename,
      start_id: startId,
      playback_id: playbackId,
      source_entry_id: sourceEntryId,
      source_channel: sourceChannel,
      // Where this row sits inside the recording; the backend adds it to the seek.
      offset: offset,
      start,
      end,
      seek: Math.max(0, Math.floor(seek)),
      route,
      format,
    });
    // An HLS session is addressed by its own unguessable token and says so, because iOS
    // hands playback to the system player, which sends no Home Assistant credentials and
    // resolves each segment against the playlist's own URL.
    const url = result.sign === false ? result.path : await this.signPath(result.path);
    return { ...result, url };
  }

  /**
   * Why the last server-side conversion produced nothing, if the backend classified one.
   *
   * A converted route is a URL handed to the video element, so a 502 from it arrives here
   * as a numeric `MediaError` with the server's explanation discarded. This asks for the
   * explanation separately, and is the only way the panel can say anything more useful
   * than "this clip cannot be played".
   */
  async playbackFailure() {
    try {
      const result = await this.hass.callWS({ type: `${DOMAIN}/playback_failure` });
      return result?.failure || null;
    } catch (err) {
      // Diagnostic only: never let this turn a failed clip into a broken panel.
      // eslint-disable-next-line no-console
      console.debug("Reolink Stamina: could not read the playback failure", err);
      return null;
    }
  }
}
