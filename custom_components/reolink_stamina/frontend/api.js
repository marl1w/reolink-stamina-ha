/** Thin wrapper over Home Assistant's websocket connection. */

const DOMAIN = "reolink_stamina";

export class StaminaApi {
  constructor(hass) {
    this.hass = hass;
  }

  /** Discovered NVRs plus the integration's options. Cheap; no NVR round trip. */
  async nvrs() {
    return this.hass.callWS({ type: `${DOMAIN}/nvrs` });
  }

  /**
   * Subscribe to event rows. The first message is a snapshot built from cache and
   * arrives immediately; `patch` messages follow as the NVR answers.
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

  /** The unsigned proxy path for one recording. */
  async playbackPath({ entryId, channel, stream, filename, startId, endId }) {
    return this.hass.callWS({
      type: `${DOMAIN}/playback_url`,
      entry_id: entryId,
      channel,
      stream,
      filename,
      start_id: startId,
      end_id: endId,
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
   * The NVR only tags a whole segment, so this comes from Home Assistant's recorder.
   * It is what lets playback open just before the event rather than at the start of a
   * long segment.
   */
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
   * The recorder serves FLV, which the browser demuxes itself — the same thing the
   * recorder's own web player does. Home Assistant only passes the bytes through, so no
   * transcoding happens and there is no server-side process to outlive the clip.
   *
   * Seeking is server-side: a different offset means a different stream.
   */
  async streamUrl({
    entryId,
    channel,
    stream,
    filename = "",
    startId = "",
    playbackId = "",
    offset = 0,
    start = "",
    end = "",
    seek = 0,
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
      // Where this row sits inside the recording; the backend adds it to the seek.
      offset: offset,
      start,
      end,
      seek: Math.max(0, Math.floor(seek)),
    });
    return { ...result, url: await this.signPath(result.path) };
  }

  /**
   * Signed, directly playable URLs for a recording, best candidate first.
   *
   * Which request type a recorder accepts varies by model and firmware, so the backend
   * returns an ordered fallback chain and the player works down it on error.
   */
  async playbackUrl(args) {
    const result = await this.playbackPath(args);
    const candidates = result.candidates || [{ vod_type: result.vod_type, path: result.path }];
    const signed = await Promise.all(
      candidates.map(async (candidate) => ({
        vodType: candidate.vod_type,
        url: await this.signPath(candidate.path),
      }))
    );
    return { candidates: signed, mime: result.mime };
  }
}
