/**
 * Turning a recording segment into the clip worth watching.
 *
 * Only ever applied to 24/7 footage. Where a camera records on events the recorder has
 * already cut the clip to the event and written its pre-record buffer into the front of it,
 * so trimming again would cut into exactly the footage the panel exists to preserve — the
 * caller is what knows which kind this is.
 *
 * Pure arithmetic, kept away from the player so the rules can be read in one place.
 */

/**
 * Bounds on trimming a segment down to its detections.
 *
 * Below the first, the clip is too short to be worth watching as one; below the second,
 * the trim saves so little that it only costs the user the rest of the segment.
 */
export const MIN_CLIP_SECONDS = 8;
export const MIN_CLIP_SAVING = 20;

/**
 * The stretch of a segment worth playing: the detections in it, plus a margin.
 *
 * Returns null when there is nothing to gain — no detections, no room, or a window already
 * about as short as the trim would make it. A mode that changes nothing is worse than no
 * mode.
 */
export function clipWindow({ duration, detections, lead = 0, tail = 0 }) {
  if (!(duration > 0)) return null;

  // Detections inside this window only: the backend looks slightly further back, so a
  // detection belonging to the previous segment can appear with a negative offset.
  const inside = (detections || []).filter(
    (d) => d.offset < duration && (d.end_offset ?? d.offset) > 0
  );
  if (inside.length === 0) return null;

  const first = Math.min(...inside.map((d) => d.offset));
  const last = Math.max(...inside.map((d) => d.end_offset ?? d.offset));

  const start = Math.max(0, first - lead);
  const end = Math.min(duration, last + tail);
  if (end - start < MIN_CLIP_SECONDS) return null;
  if (duration - (end - start) < MIN_CLIP_SAVING) return null;

  return { start, end };
}

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
