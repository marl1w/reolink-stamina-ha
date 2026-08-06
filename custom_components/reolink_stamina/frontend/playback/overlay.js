/**
 * The message over the video: what is happening, or why nothing is.
 *
 * Its own object because it is the player's only way of speaking, and it is spoken to from
 * everywhere — opening, seeking, changing route, saving, failing. Keeping the wording in
 * the callers and the mechanics here is what stops half of them rebuilding the spinner.
 */

import { el, icon } from "../dom.js";

export const OVERLAY_STYLES = /* css */ `
.overlay {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #fff;
  text-align: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.45);
  gap: 10px;
}
.overlay[hidden] { display: none; }
.overlay__box { display: flex; flex-direction: column; align-items: center; gap: 10px; max-width: 40ch; }
.overlay__text { font-size: 0.88rem; line-height: 1.5; opacity: 0.9; }
/* Larger than the 16px one used inline elsewhere: over a black stage it is the only sign
   that anything is happening, and a slow route can take several seconds to draw. */
.overlay .spinner { width: 30px; height: 30px; border-width: 3px; }
/* A bare div left the spinner and Cancel stacked against the left edge. */
.overlay__action { display: flex; align-items: center; justify-content: center; gap: 12px; }
.overlay__action:empty { display: none; }

.big-play {
  width: 64px; height: 64px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.16);
  display: grid; place-items: center;
  color: #fff;
  backdrop-filter: blur(4px);
}
.big-play .icon { --mdc-icon-size: 34px; width: 34px; height: 34px; }
.big-play:hover { background: rgba(255, 255, 255, 0.26); }
`;

export class Overlay {
  /** Build the element; `onPlay` is what the big play button calls. */
  constructor({ onPlay }) {
    this._onPlay = onPlay;
    /** True while what is shown is only the loading spinner, with nothing to say. */
    this._busy = false;
    this._icon = icon("mdi:alert-circle-outline", "icon--lg");
    this._text = el("div", { class: "overlay__text" });
    this._action = el("div", { class: "overlay__action" });
    this.element = el(
      "div",
      { class: "overlay", hidden: true },
      el("div", { class: "overlay__box" }, this._icon, this._text, this._action)
    );
  }

  get hidden() {
    return this.element.hidden;
  }

  show(iconName, text, { spinner = false, play = false, cancel = null, action = null } = {}) {
    this._busy = false;
    this.element.hidden = false;
    this._text.textContent = text;
    this._action.replaceChildren();
    this._icon.style.display = iconName ? "" : "none";
    if (iconName && this._icon.tagName.toLowerCase() === "ha-icon") {
      this._icon.setAttribute("icon", iconName);
    }
    if (spinner) {
      this._action.append(el("div", { class: "spinner" }));
    }
    if (cancel) {
      this._action.append(
        el("button", { class: "btn btn--quiet", text: "Cancel", onclick: cancel })
      );
    }
    // A way out of a dead end, rather than only an explanation of it.
    if (action) {
      this._action.append(
        el("button", { class: "btn btn--primary", text: action.label, onclick: action.onClick })
      );
    }
    if (play) {
      this._action.append(
        el(
          "button",
          { class: "big-play", title: "Play", onclick: () => this._onPlay() },
          icon("mdi:play")
        )
      );
    }
  }

  /**
   * Update the wording without rebuilding anything.
   *
   * Progress arrives several times a second, and `show` replaces the spinner element each
   * time — which restarts its CSS animation from zero, so it sat there apparently frozen
   * for the entire download.
   */
  setText(text) {
    this._text.textContent = text;
  }

  hide() {
    this._busy = false;
    this.element.hidden = true;
  }

  /**
   * Show a bare spinner: something is loading and there is nothing to explain.
   *
   * Kept apart from `show` so that clearing it cannot wipe a message that matters — a
   * conversion notice, or the reason a clip would not play.
   */
  busy() {
    this.show(null, "", { spinner: true });
    this._busy = true;
  }

  /** Hide the spinner, but only if that is all that is on screen. */
  clearBusy() {
    if (this._busy) this.hide();
  }
}
