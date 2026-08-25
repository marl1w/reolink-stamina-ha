/**
 * Writing the clip on screen out as an MP4.
 *
 * Always from the recorder's own bytes, and always the original footage: the clip is
 * assembled in the browser from the playback stream, so what lands in your downloads is
 * what the camera recorded, whatever route the *player* had to take to show it to you.
 * Adaptive playback's conversions exist for watching and touch nothing here.
 *
 * Asking the recorder to cut the clip itself would be faster — and it does have a command
 * for it — but see fragments.py: on the recorders tested it prepares a fragment it then
 * refuses to serve. Until that changes, this is the route that works.
 */

import { downloadClip } from "../clip.js";
import { el, icon } from "../dom.js";
import { formatClock, streamLabel } from "../format.js";

/**
 * A ceiling on a clip download, because it is assembled in memory.
 *
 * The low-resolution stream runs about 1 Mbit/s, so this is roughly forty minutes of it —
 * far more than any clip — while still refusing to fill a phone's memory with a
 * high-resolution recording someone asked for by accident.
 */
export const MAX_CLIP_BYTES = 300 * 1024 * 1024;

export const SAVE_STYLES = /* css */ `
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
`;

/** Hand a finished clip to the browser's downloads. */
export function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  // Give the download a moment to start before the bytes are released.
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

/** `Front_door_2026-08-03-14-00-00_sub.mp4` — camera and moment, in that order. */
export function clipFileName(event, stream) {
  const stamp = event.start.slice(0, 19).replace(/[:T]/g, "-");
  return `${event.camera.replace(/[^\w-]+/g, "_")}_${stamp}_${stream}.mp4`;
}

/**
 * The contents of the save menu: every resolution the clip exists in.
 *
 * Whichever is chosen, what gets written is the clip on screen — trimmed if the bar is
 * trimmed, the whole segment if not — never the half-hour recording it lives in.
 */
export function saveMenuNodes({ streams, length, onChoose }) {
  return [
    el("div", { class: "pop__title" }, `Save ${formatClock(length)} as MP4`),
    ...streams.map((stream) => {
      // High resolution is H.265 on every Reolink device seen so far, and Apple's own
      // players will not open the H.265 they produce: the file is valid and plays in VLC
      // and in browsers, but QuickTime and Preview load it forever. Their encoder
      // leaves the stream's scan-type and constraint flags unset, and nothing about how the
      // file is packaged changes that — ffmpeg's own remux of the same footage stalls
      // identically. Better said here than discovered after a long download.
      const hevc = stream === "main";
      return el(
        "button",
        { class: "save-option", onclick: () => onChoose(stream) },
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
    }),
  ];
}

/**
 * Read the playback stream and build the MP4 here.
 *
 * NVRs use the pass-through route and are packaged in the browser. A Home Hub already
 * serves MP4; a whole event is saved directly, while a trimmed event is cut by ffmpeg.
 */
export async function assembleClip({ api, event, stream, bounds, signal, onProgress }) {
  const { start, end } = bounds;
  const seconds = Math.max(1, Math.round(end - start));
  const file = event.files?.[stream] || { name: "", start_id: "", playback_id: "", offset: 0 };
  const request = {
    entryId: event.entry_id,
    channel: event.channel,
    stream,
    filename: file.name,
    startId: file.file_start_id || file.start_id,
    playbackId: file.playback_id,
    sourceEntryId: file.source_entry_id ?? null,
    sourceChannel: file.source_channel ?? null,
    offset: file.offset || 0,
    start: event.start,
    end: event.end,
    seek: Math.floor(start),
  };

  let source = await api.streamUrl(request);
  const wholeFile = start <= 0 && Number(event.duration) > 0 && end >= Number(event.duration);
  if (source.file && !wholeFile) {
    source = await api.streamUrl({ ...request, route: "remux", duration: seconds });
  }

  return downloadClip(source.url, {
    seconds,
    maxBytes: MAX_CLIP_BYTES,
    signal,
    onProgress: ({ seconds: reached }) =>
      onProgress(Math.min(100, Math.round((reached / seconds) * 100))),
  });
}
