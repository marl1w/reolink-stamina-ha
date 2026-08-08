/**
 * How the panel divides itself between the list and the player.
 *
 * Its own module, and pure, because the whole of the behaviour is in the numbers: how much
 * of the panel the player may take, how little the rows will settle for, and the point below
 * which the two simply do not fit side by side. Kept out of the panel so they can be
 * exercised without a browser.
 *
 * Two decisions live here.
 *
 * Whether to split at all is asked of the panel's own box rather than the window's, which is
 * the honest question: Home Assistant's sidebar is docked outside the panel, so a media query
 * measuring the viewport reports a couple of hundred pixels the panel never had. A panel that
 * is taller than it is wide is not split either, whatever its width — a portrait window is the
 * shape where a column of rows and a video side by side leaves neither of them usable, so the
 * player opens over the list instead, the way it always has on a phone.
 *
 * How wide the player is, once split, is the user's to drag — bounded at both ends. The floor
 * is the player's own chrome, which stops reading as a player somewhere below this. The
 * ceiling is the promise this module exists to keep: the rows never give up more than they
 * can spare, so dragging the divider all the way over leaves a list, not a sliver.
 */

/** The rows keep at least this much, however far the divider is dragged. */
export const LIST_MIN = 380;
/** Below this the player is chrome with a stamp-sized picture in it. */
export const PLAYER_MIN = 360;
/**
 * Narrower than this and the player opens over the list instead.
 *
 * Comfortably above `LIST_MIN + PLAYER_MIN`: that sum is what a split can be squeezed into
 * once you are in one, not what makes one worth opening. This is the width the panel has
 * always used for that call.
 */
export const SPLIT_MIN_WIDTH = 900;
/** The divider's hit area. Exported so the panel's CSS and its arithmetic cannot disagree. */
export const GUTTER = 9;

/** What the player opens at, before anyone has dragged anything. */
const OPENING_SHARE = 0.46;
/** And what that share is capped at, so the picture does not run away with a wide screen. */
const OPENING_MAX = 760;

const WIDTH_KEY = "reolink_stamina.player_width";

/** Whether the panel, at this size, can hold the list and the player side by side. */
export function splitFits(width, height) {
  return width >= SPLIT_MIN_WIDTH && width >= height;
}

/**
 * Hold a player width to what the panel can actually give it.
 *
 * `total` is the space the two panes share — the panel's width, less the divider between
 * them. The floor wins if a panel is somehow too small for both minimums, which a split at
 * `SPLIT_MIN_WIDTH` never is; it is here so this always answers with a usable number.
 */
export function clampPlayerWidth(desired, total) {
  const most = Math.max(PLAYER_MIN, total - LIST_MIN);
  return Math.round(Math.min(Math.max(desired, PLAYER_MIN), most));
}

/** The width the player takes when it has not been given one. */
export function openingPlayerWidth(total) {
  return clampPlayerWidth(Math.min(total * OPENING_SHARE, OPENING_MAX), total);
}

/**
 * The width this browser last settled on, or null for the opening one.
 *
 * Stored in pixels rather than as a share of the panel, so that docking the sidebar or
 * resizing the window takes the difference out of the list and leaves the picture the size
 * it was set to.
 */
export function readPlayerWidth() {
  try {
    const saved = Number(localStorage.getItem(WIDTH_KEY));
    return Number.isFinite(saved) && saved > 0 ? saved : null;
  } catch {
    return null;
  }
}

/** Remember a dragged width, or forget it again when the divider is reset. */
export function savePlayerWidth(width) {
  try {
    if (width === null) localStorage.removeItem(WIDTH_KEY);
    else localStorage.setItem(WIDTH_KEY, String(Math.round(width)));
  } catch {
    // Private browsing or a full quota: the divider simply starts over next time.
  }
}
