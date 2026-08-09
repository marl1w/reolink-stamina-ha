/**
 * How a recording reaches this browser, and what to try when it does not.
 *
 * Three routes, cheapest first. The panel walks down them and stops at the first that
 * actually draws a frame:
 *
 * 1. `stream` — the recorder's own FLV, demuxed in the browser. No server work at all, and
 *    what most clips on a desktop browser never leave.
 * 2. `remux` — ffmpeg changes the container and nothing else. This is what an iPhone needs
 *    for an H.264 recording: the phone's own decoder still does the work.
 * 3. `transcode` — the only route that re-encodes, for a codec the device itself cannot
 *    decode. In practice that means H.265 anywhere but a very recent Safari.
 *
 * When all three fail there is no fourth: the recorder's own whole-file download used to sit
 * here, and it was never worth having — unseekable, minutes long, and carrying the very
 * codec the browser had just refused. Saying "download this clip and watch it locally" is
 * both honest and what the download button already does properly.
 *
 * Nothing here touches the DOM beyond asking the browser what it can play, so the ladder
 * and the memory of it can be reasoned about — and tested — on their own.
 */

export const ROUTE_STREAM = "stream";
export const ROUTE_REMUX = "remux";
export const ROUTE_TRANSCODE = "transcode";

/** Routes that are converted server-side, and so cost the machine something to run. */
export const CONVERTED_ROUTES = new Set([ROUTE_REMUX, ROUTE_TRANSCODE]);

/**
 * What to tell the user about the route a clip is arriving on.
 *
 * Every route says something, the cheap one included: "Direct play" is the good news, and
 * knowing it is what makes the other two legible as the exception rather than the norm.
 */
export const ROUTE_LABELS = {
  [ROUTE_STREAM]: {
    icon: "mdi:flash-outline",
    badge: "Direct play",
    hint: "This recording is playing straight from the recorder — Reolink Stamina only forwards the bytes and this browser does the rest. Nothing is converted, and it costs the machine nothing.",
  },
  [ROUTE_REMUX]: {
    icon: "mdi:package-variant-closed",
    badge: "Repackaged by Reolink Stamina",
    hint: "This browser could not play the recorder's own stream, so Reolink Stamina is repackaging it. The video itself is untouched — nothing is re-encoded.",
  },
  [ROUTE_TRANSCODE]: {
    icon: "mdi:cog-sync-outline",
    badge: "Re-encoded by Reolink Stamina",
    hint: "This browser cannot decode this recording's codec, so Reolink Stamina is converting it to H.264 as it plays. Capped at 1080p, and only for watching — downloads keep the original.",
  },
};

/**
 * Whether this browser can decode H.265/HEVC.
 *
 * Reolink devices commonly encode the main stream as HEVC and the sub stream as H.264 —
 * commonly, not always: some models and firmware serve HEVC on both, which is why nothing
 * here decides by resolution and every route is chosen from what the stream turns out to
 * contain. Safari plays HEVC; Chrome and Firefox generally do not. Getting this wrong looks
 * like "the device is broken" when the bytes arrived perfectly well.
 */
export const HEVC_SUPPORTED = (() => {
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
 * Which container a server-converted stream should arrive in, for *this* browser.
 *
 * HLS wherever the browser plays it natively, which means every Apple one. That is not a
 * preference: iOS has no `MediaSource` at all, and Safari refuses a progressive stream
 * whose length is unknown and whose server cannot answer range requests — which any
 * live-paced route is. HLS is the only thing it will take, and it is what makes playback
 * work in the Home Assistant app on an iPhone.
 *
 * Everywhere else, one chunked fragmented-MP4 response, which needs no session at all.
 */
const PROBED_FORMAT = (() => {
  try {
    const probe = document.createElement("video");
    if (probe.canPlayType("application/vnd.apple.mpegurl") !== "") return "hls";
  } catch {
    // Fall through to MP4.
  }
  return "mp4";
})();

/**
 * Set once a browser that claimed HLS has refused to open one.
 *
 * `canPlayType` answers "maybe", and a browser that overstates it takes the entire ladder
 * down with it: both conversions are handed a playlist nothing here can read, every rung
 * fails for a reason that has nothing to do with the recording, and the player ends up
 * blaming the codec. One refusal is enough to stop believing the claim.
 *
 * Deliberately not remembered across page loads. A playlist can also fail to open because
 * the session behind it went away, and one wasted attempt per page load is a far cheaper
 * mistake than pinning a browser that genuinely does play HLS — every iPhone — onto a
 * container it has no way to use.
 */
let hlsRefused = false;

/** Stop asking this browser for HLS, having just watched it refuse one. */
export function refuseHls() {
  hlsRefused = true;
}

/** Which container a server-converted stream should arrive in, as things currently stand. */
export function convertedFormat() {
  return hlsRefused ? "mp4" : PROBED_FORMAT;
}

/**
 * Whether this browser plays HLS itself, rather than being handed one long MP4.
 *
 * The same answer as `convertedFormat`, named for the other thing it decides. A browser with
 * its own HLS pipeline has two separate routes to a decoder — that one, and Media Source
 * Extensions — and they do not support the same codecs. Which is why repackaging is worth
 * trying there even for a codec that just failed.
 */
export function nativeHls() {
  return convertedFormat() === "hls";
}

/**
 * The next route to try after this one failed, or null when there is nothing left.
 *
 * The two conversions follow the recorder's own stream, cheapest first.
 *
 * `decodeFailure` skips the repackaging step, and the distinction it draws is the one that
 * matters most here: repackaging fixes a container the browser could not *read*, and can do
 * nothing at all about a codec it could not *decode* — the bitstream comes out the other
 * side unchanged. So it is only worth trying when the decoder was not the thing that failed.
 *
 * Except where the container decides which decoder gets the stream, which is the whole of
 * Safari. It was read the other way round for a while, and the cost was the worst case this
 * integration has: Safari demuxes an H.265 recording through Media Source Extensions, claims
 * it, then stalls — so every high-resolution clip skipped straight to a full software
 * re-encode, the single most expensive thing here, on the browser least likely to need it.
 *
 * What that missed is that repackaging does not hand the same stream to the same decoder.
 * Safari's native HLS pipeline is not its MSE one: it plays HEVC in fragmented MP4 with
 * hardware decoding, which is exactly why segments are written as fragmented MP4 in the first
 * place. So where the browser has that second pipeline, a decode failure is a reason to try
 * repackaging, not a reason to skip it — the stalled decoder is the one being left behind.
 */
export function nextRoute(current, { decodeFailure = false } = {}) {
  switch (current) {
    case ROUTE_STREAM:
      return decodeFailure && !nativeHls() ? ROUTE_TRANSCODE : ROUTE_REMUX;
    case ROUTE_REMUX:
      return ROUTE_TRANSCODE;
    default:
      return null;
  }
}

/**
 * What worked last time, and for what.
 *
 * Walking the ladder costs a failed attempt, and the answer barely ever changes: which
 * codec a stream carries is a setting on the recorder, not a state. So the winning route is
 * remembered and used directly next time, and the conversion is only ever *discovered*
 * once. localStorage is already per browser, so the keys only have to name the stream.
 *
 * Two levels, because the useful generalisation is by resolution:
 *
 * * A **hint per resolution** — "high resolution needed converting here" — which is what a
 *   camera opened for the first time starts from. Recorders are configured as a whole, so
 *   what the main stream needs on one camera is almost always what it needs on the next.
 * * An **entry per camera and resolution**, which overrides the hint. This is what keeps a
 *   camera that plays perfectly well from being converted for ever because a *different*
 *   camera on the same recorder needed it.
 *
 * Only conversion updates the hint: passthrough is the default anyway, and one camera
 * playing natively says nothing about the others. Both expire eventually, so a recorder
 * switched to H.264 is re-examined rather than converted for the rest of time.
 */
// Versioned: an earlier build decided a route "worked" from the video element's reported
// dimensions, which Safari fills in from a track header it never manages to decode. Those
// entries pinned a black window in place, so they are abandoned rather than trusted.
const ROUTE_MEMORY_KEY = "reolink_stamina.routes.v2";
// Which release of the integration wrote what is currently in that key.
const ROUTE_RELEASE_KEY = "reolink_stamina.routes.release";
const ROUTE_MEMORY_TTL = 90 * 24 * 3600 * 1000;
const ROUTE_MEMORY_MAX = 200;
const HINT_PREFIX = "*|";

/** The per-camera key and the per-resolution hint key for one row. */
export function routeMemoryKeys(event, stream) {
  return {
    camera: `${event.entry_id}|${event.channel}|${stream}`,
    hint: `${HINT_PREFIX}${stream}`,
  };
}

export function readRouteMemory() {
  try {
    const saved = JSON.parse(localStorage.getItem(ROUTE_MEMORY_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

export function writeRouteMemory(memory) {
  try {
    localStorage.setItem(ROUTE_MEMORY_KEY, JSON.stringify(memory));
  } catch {
    // Private browsing or a full quota: the ladder is simply walked again next time.
  }
}

/**
 * Forget everything remembered under an earlier release, and note the one running now.
 *
 * Which codec a recording carries is a setting on the recorder, which is why remembering the
 * answer is worth anything at all. But which route *works* is not only about the recording:
 * it is equally about this integration's own conversions, and those change with every
 * release. A build that fixes repackaging cannot undo a browser's memory of repackaging
 * failing — that browser skips the rung that now works and keeps paying for the re-encode
 * below it, for the rest of the ninety days the entry lives. "Still converting after the
 * update" is the shape of every one of those.
 *
 * So an update is treated as invalidating the ladder rather than any single entry: the panel
 * cannot tell which of its answers a given release changed, and the whole memory is worth
 * less than one wrong answer that outlives its fix. What it costs is one walk down the ladder
 * per camera afterwards — exactly what a browser that has never played anything pays — and
 * it is paid once instead of being carried for a quarter.
 *
 * Called for its effect on storage, once, before anything reads the memory back.
 */
export function forgetRoutesFromEarlierRelease(release) {
  if (!release) return false;
  try {
    if (localStorage.getItem(ROUTE_RELEASE_KEY) === release) return false;
    localStorage.removeItem(ROUTE_MEMORY_KEY);
    localStorage.setItem(ROUTE_RELEASE_KEY, release);
    return true;
  } catch {
    // Private browsing or a full quota: there was nothing durable to forget.
    return false;
  }
}

function liveEntry(memory, key, now) {
  const entry = memory[key];
  if (!entry || typeof entry.route !== "string") return null;
  return now - (entry.at || 0) < ROUTE_MEMORY_TTL ? entry : null;
}

/** The route to start on for this camera and resolution, or null for passthrough. */
export function recalledRoute(memory, keys, now = Date.now()) {
  const exact = liveEntry(memory, keys.camera, now);
  if (exact) return exact.route;
  const hint = liveEntry(memory, keys.hint, now);
  return hint ? hint.route : null;
}

/** Record what worked, against this camera and — for a conversion — its resolution. */
export function rememberRoute(memory, keys, route, now = Date.now()) {
  memory[keys.camera] = { route, at: now };
  if (route !== ROUTE_STREAM) memory[keys.hint] = { route, at: now };

  // Oldest first, and never the resolution hints, so a long-lived browser cannot grow this
  // without bound while still generalising to a camera it has not seen before.
  const cameras = Object.keys(memory).filter((key) => !key.startsWith(HINT_PREFIX));
  if (cameras.length > ROUTE_MEMORY_MAX) {
    cameras
      .sort((a, b) => (memory[a].at || 0) - (memory[b].at || 0))
      .slice(0, cameras.length - ROUTE_MEMORY_MAX)
      .forEach((stale) => delete memory[stale]);
  }
  return memory;
}
