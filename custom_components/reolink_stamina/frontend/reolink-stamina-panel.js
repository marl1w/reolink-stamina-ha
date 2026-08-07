/**
 * Reolink Stamina — sidebar panel entry point.
 *
 * Registered by the integration via panel_custom, so this runs inside the Home
 * Assistant frontend with the real `hass` object: websocket calls, URL signing and
 * theming all go through Home Assistant directly, with no extra hop.
 */

import { adoptStyles, el, frameDebounce, icon } from "./dom.js";
import { SHARED } from "./theme.js";
import { ToolbarFold } from "./fold.js";
import { StaminaApi } from "./api.js";
import { StaminaStore } from "./store.js";
import { forgetRoutesFromEarlierRelease } from "./playback/routes.js";
import "./views/device-picker.js";
import "./views/toolbar.js";
import "./views/event-list.js";
import "./views/player.js";

const STYLES = /* css */ `
:host {
  display: flex;
  flex-direction: column;
  /* dvh rather than vh, which is what Home Assistant itself uses for a panel. On a phone
     the two differ by the height of the browser's own chrome, and a panel that much taller
     than the window is what put a scrollbar on a page with nothing to scroll. The vh line
     stays as the fallback for engines that do not know dvh. */
  height: 100vh;
  height: 100dvh;
  background: var(--rv-bg);
  overflow: hidden;
  /* Nothing here scrolls the page, so a swipe that runs past the end of a list should not
     rubber-band the whole panel away from under the status bar. */
  overscroll-behavior: none;
}

/*
 * No safe-area inset here, deliberately.
 *
 * The panel element sits in Home Assistant's own layout, and in the Companion app that
 * layout already starts below the status bar — so a header that also held itself off it
 * would be inset twice, which is exactly how it looked. The full-screen player below is
 * the opposite case and does need its own: a fixed position measures from the viewport,
 * so it escapes that offset and lands under the status bar on its own.
 */
.app-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 8px 0 16px;
  height: var(--header-height, 56px);
  flex: 0 0 auto;
  background: var(--app-header-background-color, var(--rv-accent));
  color: var(--app-header-text-color, #fff);
  box-shadow: var(--rv-shadow);
  z-index: 4;
}
.app-header__title { font-size: 1.15rem; font-weight: 500; margin-left: 8px; }
.app-header .icon-btn { color: inherit; }
/* The menu button only exists on narrow screens, so it takes the space the header's
   own left padding was holding rather than adding to it. */
.app-header:has(.menu-btn:not([hidden])) { padding-left: 4px; }
.menu-btn[hidden] { display: none; }

.content { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }

.split { flex: 1 1 auto; min-height: 0; display: flex; }
.pane-list { flex: 1 1 auto; min-width: 0; }
.pane-player {
  flex: 0 0 46%;
  max-width: 760px;
  min-width: 360px;
  border-left: 1px solid var(--rv-line);
  display: flex;
  animation: rv-slide-in 220ms var(--rv-ease) both;
}
.pane-player[hidden] { display: none; }
@keyframes rv-slide-in { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: none; } }

reolink-event-player { flex: 1 1 auto; min-width: 0; }

/* On narrow screens the player takes over the screen instead of squeezing the list. */
@media (max-width: 900px) {
  .pane-player {
    position: fixed;
    inset: 0;
    z-index: 10;
    flex: 1 1 auto;
    max-width: none;
    min-width: 0;
    border-left: none;
    background: var(--rv-surface);
    /* Covering the screen means covering the notch and the home indicator too. The player
       is above even the panel's own header here, so nothing else is holding either off it,
       and its chrome is fixed at both edges: the clip's title would sit under the clock and
       the play button under the home indicator. */
    padding: var(--rv-safe-top) var(--rv-safe-right) var(--rv-safe-bottom) var(--rv-safe-left);
    animation: rv-slide-up 220ms var(--rv-ease) both;
  }
  @keyframes rv-slide-up { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
}
`;

class ReolinkStaminaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
    this._store = null;
    this._render = frameDebounce(() => this._doRender());
    this._onKeydown = (event) => {
      if (event.key !== "Escape" || !this._store?.selectedEventId) return;
      // While the player fills the screen, Escape is how you come back out of it — the
      // browser handles that, and closing the clip as well would be one press too many.
      if (document.fullscreenElement || document.webkitFullscreenElement) return;
      this._closePlayer();
    };
  }

  connectedCallback() {
    this.addEventListener("keydown", this._onKeydown);
  }

  disconnectedCallback() {
    this.removeEventListener("keydown", this._onKeydown);
    this._store?.destroy();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._api) this._api.hass = hass;
    this._syncMenuButton();
    if (!this._store) this._start();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this._syncMenuButton();
  }

  set panel(panel) {
    this._panel = panel;

    // The integration's own version, handed over with the panel registration. Anything this
    // browser learned about how a recording reaches it was learned from a different build's
    // conversions, so an update starts that question again rather than carrying an answer a
    // release may have just fixed. Cheap, and idempotent: this setter can run more than once.
    forgetRoutesFromEarlierRelease(panel?.config?.version);
  }

  async _start() {
    this._api = new StaminaApi(this._hass);
    this._store = new StaminaStore(this._api);
    this._store.addEventListener("changed", () => this._render());
    this._build();
    await this._store.init();
    this._render();
  }

  _build() {
    if (this._built) return;

    // Home Assistant does not put a toolbar around a custom panel, so the sidebar
    // toggle has to live here or there is no way back to the sidebar on a phone.
    // Firing `hass-toggle-menu` is what <ha-menu-button> does; going through the
    // event instead of the element keeps this working even when that element has
    // not been loaded into the frontend yet.
    this._menuBtn = el(
      "button",
      {
        class: "icon-btn menu-btn",
        title: "Menu",
        hidden: true,
        onclick: () => {
          this.dispatchEvent(
            new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })
          );
        },
      },
      icon("mdi:menu")
    );

    const header = el(
      "header",
      { class: "app-header" },
      this._menuBtn,
      el("div", { class: "app-header__title", text: "Reolink Stamina" })
    );

    // Setup view
    this._picker = el("reolink-device-picker", { class: "scroll", style: { overflow: "auto", flex: "1" } });

    // Main view
    this._toolbar = el("reolink-stamina-toolbar");
    this._list = el("reolink-event-list", { class: "pane-list scroll" });
    this._player = el("reolink-event-player");
    this._playerPane = el("div", { class: "pane-player", hidden: true }, this._player);

    // Reading down the list folds the toolbar away; a flick back up brings it back. Whether
    // that attribute means anything is the toolbar's CSS to decide — on a wide screen there
    // is room for both. Passive, so this can never hold up a scroll.
    this._fold = new ToolbarFold();
    this._collapsed = false;
    this._list.addEventListener("scroll", () => this._onListScroll(), { passive: true });

    this._list.addEventListener("event-selected", (event) => {
      this._store.selectEvent(event.detail.id);
    });
    this._player.addEventListener("close-player", () => this._closePlayer());
    this._player.addEventListener("step-event", (event) => {
      if (this._store.stepEvent(event.detail)) {
        this._list.focusEvent(this._store.selectedEventId);
      }
    });

    this._main = el(
      "div",
      { class: "content" },
      this._toolbar,
      el("div", { class: "split" }, this._list, this._playerPane)
    );

    this._body = el("div", { class: "content" });
    this.shadowRoot.append(header, this._body);
    this._built = true;
    this._syncMenuButton();
  }

  /**
   * Show the sidebar toggle only when the sidebar is out of reach: on a narrow screen,
   * or when the user has collapsed it on a wide one. Kept out of `_doRender` because
   * `hass` is reassigned on every state change and this must not drag a full re-render
   * of the timeline along with it.
   */
  _syncMenuButton() {
    if (!this._menuBtn) return;
    this._menuBtn.hidden = !(this._narrow || this._hass?.dockedSidebar === "always_hidden");
  }

  /**
   * Give the list the screen while it is being read.
   *
   * The decision itself is `fold.js`; all that happens here is handing it a position and
   * putting the answer on the toolbar. Guarded against restating an answer that has not
   * changed, because a scroll reports several times a frame and toggling the attribute is
   * what drives the transition.
   */
  _onListScroll() {
    const collapsed = this._fold.update(this._list.scrollTop);
    if (this._collapsed === collapsed) return;
    this._collapsed = collapsed;
    this._toolbar.toggleAttribute("collapsed", collapsed);
  }

  _closePlayer() {
    this._store.selectEvent(null);
  }

  _doRender() {
    const store = this._store;
    if (!store || !this._built) return;

    // Swap between the setup view and the timeline.
    const showSetup = !store.setupDone;
    const wanted = showSetup ? this._picker : this._main;
    if (this._body.firstElementChild !== wanted) {
      this._body.replaceChildren(wanted);
    }

    if (showSetup) {
      this._picker.store = store;
      return;
    }

    this._toolbar.store = store;
    this._list.store = store;

    // Player follows the selection. A selection can disappear when filters change,
    // in which case the player closes rather than showing a clip that is no longer
    // in the list.
    const selected = store.selectedEvent;
    this._playerPane.hidden = !selected;
    this._player.store = store;
    this._player.api = this._api;
    if (selected) {
      this._player.setEvent(selected);
      this._player.syncFromStore();
    } else {
      this._player.setEvent(null);
    }
  }
}

// Guarded: Home Assistant may import this module more than once (after an
// update, or from a cached copy), and a duplicate define() throws and takes the
// whole panel down with it.
if (!customElements.get("reolink-stamina-panel")) customElements.define("reolink-stamina-panel", ReolinkStaminaPanel);
