/**
 * Where one score sits on the "more common ... unusual" scale the sheet draws.
 *
 * Its own module because it is arithmetic, and because getting it wrong is invisible: the
 * gauge used to derive its extent from the score it was about to plot —
 *
 *     const extent = Math.max(Math.abs(score), Math.abs(threshold), 1) * 1.15;
 *
 * — which cancels. Any score past the threshold put the needle at exactly 50 ± 50/1.15, so a
 * -0.8 and a -10.4 drew the identical picture, and the shaded threshold region slid up and
 * down the track instead, because it was measured against that same moving extent. Every
 * event looked equally unusual while the breakdown underneath it was perfectly correct.
 *
 * The fix is the thing a scale has to be: shared. One extent for every event in the window,
 * so the needle moves between events and the threshold stays where the eye left it.
 */

/**
 * How much room to leave past the widest score, so the needle never sits on the very end.
 *
 * A mark flush against the edge reads as clipped — as though the real value were somewhere
 * off the track — which is exactly the wrong thing to say about the rarest event on a camera.
 */
export const HEADROOM = 1.15;

/**
 * The half-width of the track, in nats, for a whole window of scores.
 *
 * Every score in the window, not just the one being drawn: that is what makes two events
 * comparable, which is the only reason to draw a scale rather than print the number.
 *
 * The threshold is included so the shaded region is always on the track — a camera can be
 * marked at 1.4 while everything in the window sits below it, and a region drawn off the end
 * would say nothing can ever be marked. The 1 is a floor for the quiet case: a window whose
 * scores are all near zero would otherwise magnify a rounding difference into a full track.
 */
export function extentFor(scores, threshold) {
  const widest = scores.reduce((most, score) => Math.max(most, Math.abs(score || 0)), 0);
  const line = threshold === null || threshold === undefined ? 0 : Math.abs(threshold);
  return Math.max(widest, line, 1) * HEADROOM;
}

/**
 * Where a score lands, as a percentage across the track, with chance at the middle.
 *
 * Clamped just inside the ends rather than at them, for the same reason `HEADROOM` exists:
 * the needle has width, and one drawn on the boundary loses half of itself.
 */
export function needleAt(score, extent) {
  return Math.max(1, Math.min(99, 50 + (score / extent) * 50));
}

/**
 * Where the marked region begins, as a percentage across the track.
 *
 * Never left of the middle, whatever the threshold: a negative threshold is a real thing —
 * a camera whose quantile fell below chance — but shading the common half as "marked" would
 * claim the model marks ordinary events, and the floor in `score.py` is what actually stops
 * it doing so. Without a threshold there is nothing entitled to be marked, and the caller
 * says so by passing null: the region starts at the far end and has no width.
 */
export function cutAt(threshold, extent) {
  if (threshold === null || threshold === undefined) return 100;
  return Math.max(50, Math.min(100, 50 + (threshold / extent) * 50));
}
