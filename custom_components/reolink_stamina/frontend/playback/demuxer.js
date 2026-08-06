/**
 * The FLV demuxer, and what it can tell us.
 *
 * The recorder serves FLV, which no browser plays natively but every current desktop one
 * can demux through Media Source Extensions. Doing that here is what removes ffmpeg from
 * the server on the normal path: Home Assistant only forwards bytes.
 *
 * It ships with the integration rather than coming from a CDN, and resolves relative to
 * this module so the versioned static path busts it along with everything else.
 */

let demuxerPromise = null;

export function loadDemuxer() {
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

/**
 * Whether this browser can demux in software at all.
 *
 * False on every iPhone: iOS exposes no `MediaSource`, so there is nothing to feed.
 */
export function canDemux(demuxer) {
  try {
    return Boolean(demuxer?.isSupported?.());
  } catch {
    return false;
  }
}

/**
 * Whether a codec string from the demuxer's media info is H.265.
 *
 * Read as soon as the stream's header has been parsed, which is the earliest anything can
 * know that a browser without HEVC support is about to draw nothing at all.
 */
export function isHevcCodec(codec) {
  const name = String(codec || "").toLowerCase();
  return name.startsWith("hvc1") || name.startsWith("hev1") || name.includes("hevc");
}

/**
 * How many frames this element has actually decoded, or null where it will not say.
 *
 * The distinction is the whole game when deciding whether a route works. A stalled decoder
 * still reports a size, a duration and `readyState`, still fires `loadeddata`, and still
 * looks entirely healthy from the outside — Safari does exactly that with the H.265 these
 * recorders produce, taking the dimensions from a track header it then never decodes. Only
 * a frame count distinguishes "playing" from "black".
 *
 * Corrupted frames are subtracted: a decoder handed a bitstream it cannot make sense of can
 * count them as arrived while drawing none of them.
 */
export function decodedFrames(video) {
  try {
    if (typeof video.getVideoPlaybackQuality === "function") {
      const quality = video.getVideoPlaybackQuality();
      const total = quality?.totalVideoFrames;
      if (typeof total === "number") return total - (quality.corruptedVideoFrames || 0);
    }
  } catch {
    // Fall through to the WebKit-specific counter.
  }
  if (typeof video.webkitDecodedFrameCount === "number") return video.webkitDecodedFrameCount;
  return null;
}
