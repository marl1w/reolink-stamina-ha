/**
 * What this panel can do, shown once.
 *
 * Reolink Stamina has grown three features and five options, and none of them announce
 * themselves: a timeline that browses every recorder at once, clips copied off-site, and now
 * a record of what each camera normally sees. Somebody who installed it for the timeline has
 * no way of discovering the other two short of reading the repository.
 *
 * So it says so, once, and then never again unless asked. The rules it follows are the ones
 * that decide whether this is useful or irritating:
 *
 * - **Once per version, and only on a change.** Seen it, and it stays gone.
 * - **Never on a fresh install.** Somebody who has just chosen to install this has read
 *   something about it in the last five minutes; opening a dialog to tell them what they
 *   just read is the behaviour that teaches people to dismiss dialogs unread.
 * - **Reachable again**, because a thing you can only ever see by accident is not
 *   documentation.
 *
 * The copy is deliberately about what a person gets, not about what changed in the code. A
 * changelog belongs in the release notes; this is the panel introducing itself.
 */

import { adoptStyles, el, icon } from "../dom.js";
import { SHARED } from "../theme.js";
import { FEATURES, summarise } from "../whats-new.js";

const STYLES = /* css */ `
:host { display: contents; }

dialog {
  width: min(560px, calc(100vw - 32px));
  max-height: min(80vh, 720px);
  padding: 0;
  border: 1px solid var(--rv-line);
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  color: var(--rv-text);
  box-shadow: var(--rv-shadow-lifted);
  overflow: hidden;
}
  /*
   * The flex column and "min-height: 0" below are what make the body scroll in Safari, which
   * gives the scrolling child its full content height otherwise and lets the dialog clip it.
   *
   * Scoped to [open], and that matters: a dialog's "display: none" while closed comes from
   * the browser's own stylesheet, so setting a display here unconditionally overrides it and
   * the dialog never goes away again.
   */
dialog[open] { display: flex; flex-direction: column; }
dialog::backdrop { background: rgba(0, 0, 0, 0.42); }

.head { flex: 0 0 auto; padding: 22px 24px 4px; }
.eyebrow {
  font-size: 0.72rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--rv-text-dim); margin-bottom: 6px;
}
h2 { margin: 0; font-size: 1.24rem; font-weight: 650; letter-spacing: -0.01em; }
.sub { margin: 8px 0 0; font-size: 0.88rem; line-height: 1.5; color: var(--rv-text-dim); }

.body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 18px 24px 4px;
}

.thing { display: flex; gap: 14px; padding: 12px 0; }
.thing + .thing { border-top: 1px solid var(--rv-line); }
.thing__mark {
  width: 36px; height: 36px; border-radius: 11px; flex: 0 0 auto;
  display: grid; place-items: center;
  background: color-mix(in srgb, var(--tone, var(--rv-accent)) 16%, transparent);
  color: color-mix(in srgb, var(--tone, var(--rv-accent)) 90%, var(--rv-text));
}
.thing__mark .icon { --mdc-icon-size: 20px; width: 20px; height: 20px; }
.thing[data-tone="person"] { --tone: var(--rv-tone-person); }
.thing[data-tone="vehicle"] { --tone: var(--rv-tone-vehicle); }
.thing[data-tone="animal"] { --tone: var(--rv-tone-animal); }
.thing[data-tone="alert"] { --tone: var(--rv-tone-alert); }
.thing__body { min-width: 0; }
.thing__title { font-size: 0.94rem; font-weight: 650; margin-bottom: 3px; }
.thing__title .tag {
  margin-left: 7px; font-size: 0.66rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--rv-text-dim);
  border: 1px solid var(--rv-line); border-radius: var(--rv-radius-pill); padding: 1px 7px;
  vertical-align: 1px;
}
.thing__text { font-size: 0.85rem; line-height: 1.5; color: var(--rv-text-dim); }
.thing__where { font-size: 0.78rem; line-height: 1.5; margin-top: 5px; color: var(--rv-text-dim); }
.thing__where b { color: var(--rv-text); font-weight: 600; }

.foot {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 14px 24px 18px;
  border-top: 1px solid var(--rv-line);
  background: var(--rv-surface);
}
.version { font-size: 0.74rem; color: var(--rv-text-dim); font-variant-numeric: tabular-nums; }

/*
 * Full-screen on a phone, like the two sheets. A dialog inset by sixteen pixels on a 390px
 * screen is a box with a sliver of dimmed panel around it — the inset reads as an accident
 * rather than as a frame, and it costs the width the content actually wanted.
 *
 * Fixed rather than covering the screen by size alone: a modal dialog is placed against the
 * viewport, so it escapes the offset Home Assistant's own layout starts at and would land
 * under the status bar — the same case the full-screen player already handles. The safe-area
 * padding is what holds the heading off the clock and the "Got it" button off the home
 * indicator.
 */
@media (max-width: 700px) {
  dialog {
    position: fixed;
    inset: 0;
    width: 100vw;
    max-width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
    margin: 0;
    border: 0;
    border-radius: 0;
    padding: var(--rv-safe-top) var(--rv-safe-right) var(--rv-safe-bottom) var(--rv-safe-left);
  }
}

@media (max-width: 560px) {
  .head { padding: 18px 18px 4px; }
  .body { padding: 14px 18px 4px; }
  .foot { padding: 12px 18px 16px; }
}
`;

/** The dialog itself. What it lists, and when it opens, both live in `../whats-new.js`. */
export class WhatsNew extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
  }

  /** Build once, on first use: most sessions never open this at all. */
  _build() {
    this._version = el("span", { class: "version" });
    this._dialog = el(
      "dialog",
      { "aria-label": "What Reolink Stamina can do" },
      el(
        "div",
        { class: "head" },
        el("div", { class: "eyebrow", text: "Reolink Stamina" }),
        el("h2", { text: "What this panel can do" }),
        el("p", { class: "sub", text: summarise() })
      ),
      el(
        "div",
        { class: "body" },
        ...FEATURES.map((feature) =>
          el(
            "div",
            { class: "thing", dataset: { tone: feature.tone } },
            el("div", { class: "thing__mark" }, icon(feature.icon)),
            el(
              "div",
              { class: "thing__body" },
              el(
                "div",
                { class: "thing__title" },
                feature.title,
                feature.beta ? el("span", { class: "tag", text: "beta" }) : null
              ),
              el("div", { class: "thing__text", text: feature.text }),
              el("div", { class: "thing__where", html: feature.where })
            )
          )
        )
      ),
      el(
        "div",
        { class: "foot" },
        this._version,
        el("button", {
          class: "btn btn--primary",
          text: "Got it",
          onclick: () => this.close(),
        })
      )
    );
    this.shadowRoot.append(this._dialog);
    this._built = true;
  }

  /** Show it, remembering nothing: what to remember is the caller's business. */
  open(version) {
    if (!this._built) this._build();
    this._version.textContent = version ? `Version ${version}` : "";
    if (!this._dialog.open) this._dialog.showModal();
  }

  /** Close it, and tell whoever is listening that it has been seen. */
  close() {
    if (this._built && this._dialog.open) this._dialog.close();
    this.dispatchEvent(new CustomEvent("seen", { bubbles: true, composed: true }));
  }
}

if (!customElements.get("reolink-stamina-whats-new")) {
  customElements.define("reolink-stamina-whats-new", WhatsNew);
}
