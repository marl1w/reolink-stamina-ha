/**
 * When the toolbar folds away, and what it takes to bring it back.
 *
 * Its own module, and pure, because this is a judgement rather than a mechanism: the
 * numbers below are the whole of the behaviour, and getting them wrong is felt immediately
 * on a phone. Kept out of the panel so they can be exercised without a browser.
 *
 * The asymmetry is deliberate. Folding away is cheap and reversible, so a short push down
 * the list is enough. Coming back is not — the filters land on top of whatever is being
 * read — so it is asked for with a deliberate flick: a run that is both long and quick.
 * Reaching the top of the list always brings them back regardless, which is the way out for
 * someone scrolling in short nudges that no single run could ever add up to.
 */

/** Within this much of the top, the filters simply belong on screen. */
const COLLAPSE_AFTER = 64;
/** A downward run this long folds them away. */
const COLLAPSE_RUN = 28;
/** An upward run has to cover this far, at this average speed, to bring them back. */
const EXPAND_RUN = 160;
const EXPAND_SPEED = 0.45; // pixels per millisecond
/**
 * A pause this long ends a run.
 *
 * Without it, an upward nudge every few seconds would eventually add up to a flick that was
 * never made — and it is also what keeps iOS momentum, which arrives as a burst of events
 * and then stops, from being joined to whatever the next touch does.
 */
const RUN_GAP_MS = 220;

export class ToolbarFold {
  /** `now` is injectable so a test can drive the clock rather than wait on it. */
  constructor({ now = () => performance.now() } = {}) {
    this._now = now;
    this.collapsed = false;
    this._top = 0;
    this._at = 0;
    this._distance = 0;
    this._elapsed = 0;
  }

  /** Feed it a scroll position and it answers whether the toolbar should be folded away. */
  update(top) {
    const at = this._now();
    const delta = top - this._top;
    const since = at - this._at;
    this._top = top;
    this._at = at;

    if (top <= COLLAPSE_AFTER) {
      this._distance = 0;
      this._elapsed = 0;
      this.collapsed = false;
      return this.collapsed;
    }
    if (delta === 0) return this.collapsed;

    // A change of direction starts a new run, and so does a pause.
    const fresh = since > RUN_GAP_MS || Math.sign(delta) !== Math.sign(this._distance);
    if (fresh) {
      this._distance = 0;
      this._elapsed = 0;
    }
    this._distance += delta;
    // The gap that ended the previous run is not part of this one, so the first sample of a
    // run contributes its distance but none of its time.
    if (!fresh) this._elapsed += since;

    if (this._distance >= COLLAPSE_RUN) {
      this.collapsed = true;
    } else if (
      -this._distance >= EXPAND_RUN &&
      -this._distance / Math.max(this._elapsed, 1) >= EXPAND_SPEED
    ) {
      this.collapsed = false;
    }
    return this.collapsed;
  }
}
