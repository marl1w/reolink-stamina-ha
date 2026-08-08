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
import { shouldIntroduce } from "./whats-new.js";
import "./views/whats-new.js";
import { StaminaStore } from "./store.js";
import {
  GUTTER,
  LIST_MIN,
  PLAYER_MIN,
  clampPlayerWidth,
  openingPlayerWidth,
  readPlayerWidth,
  savePlayerWidth,
  splitFits,
} from "./split.js";
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

/*
 * Covering the screen is the default, and the side-by-side split the exception — the other
 * way round from how this used to read.
 *
 * Which one applies is data-layout on the host, set from the panel's own measured box
 * rather than a media query on the window: Home Assistant docks its sidebar outside the
 * panel, so the viewport is worth a couple of hundred pixels the panel never had. Taking
 * the overlay as the default is what makes that safe — it is the layout that works at any
 * size, so the moment before the first measurement, and any engine that cannot measure at
 * all, get a player that covers the screen rather than a split squeezed into a phone.
 */
.pane-player {
  position: fixed;
  inset: 0;
  z-index: 10;
  flex: 1 1 auto;
  display: flex;
  background: var(--rv-surface);
  /* Covering the screen means covering the notch and the home indicator too. The player
     is above even the panel's own header here, so nothing else is holding either off it,
     and its chrome is fixed at both edges: the clip's title would sit under the clock and
     the play button under the home indicator. */
  padding: var(--rv-safe-top) var(--rv-safe-right) var(--rv-safe-bottom) var(--rv-safe-left);
  animation: rv-slide-up 220ms var(--rv-ease) both;
}
.pane-player[hidden] { display: none; }
@keyframes rv-slide-up { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }

:host([data-layout="split"]) .pane-player {
  position: static;
  /* The lift above belongs to the overlay, which has a header to cover. Beside the list the
     pane covers nothing, and keeping it would outrank the toolbar's own stacking context and
     paint over an open popover hanging down from it -- z-index applies to a flex item whether
     or not it is positioned, so dropping position is not enough to drop the lift. */
  z-index: auto;
  /* The width the divider was left at. The panel keeps it inside its bounds, so the list
     always has room for a row; the fallback only stands in for the frame before it does. */
  flex: 0 0 var(--rv-player-width, 46%);
  min-width: 0;
  padding: 0;
  animation: rv-slide-in 220ms var(--rv-ease) both;
}
@keyframes rv-slide-in { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: none; } }

reolink-event-player { flex: 1 1 auto; min-width: 0; }

/* ------------------------------------------------------------------- divider */

/*
 * The line between the panes, and the handle it doubles as.
 *
 * It stands in for the border the player pane used to draw, so the seam looks the same when
 * nobody goes near it — what is new is the grip in the middle of it, which is there to say
 * the seam can be dragged at all. A hairline is too thin to aim at, so the hit area is the
 * whole ${GUTTER} pixels and the line is drawn down the middle of it.
 */
.gutter {
  flex: 0 0 ${GUTTER}px;
  position: relative;
  align-self: stretch;
  cursor: col-resize;
  /* A drag here is a drag, not the start of a scroll of the list behind it. */
  touch-action: none;
}
.gutter[hidden] { display: none; }
.gutter::before,
.gutter::after {
  content: "";
  position: absolute;
  left: 50%;
  pointer-events: none;
}
.gutter::before { top: 0; bottom: 0; width: 1px; transform: translateX(-50%); background: var(--rv-line); }
.gutter::after {
  top: 50%;
  /* Centred by transform rather than by a margin, so growing on hover grows both ways and
     the grip stays where the eye left it. */
  transform: translate(-50%, -50%);
  width: 4px;
  height: 34px;
  border-radius: var(--rv-radius-pill);
  background: color-mix(in srgb, var(--rv-text) 28%, transparent);
  transition: background 140ms var(--rv-ease), height 140ms var(--rv-ease);
}
.gutter:hover::after,
.gutter:focus-visible::after,
.gutter[dragging]::after {
  height: 56px;
  background: var(--rv-accent);
}

/*
 * While the divider is being dragged, the pointer is over the list and the player rather
 * than over the handle — so the cursor and the ban on selecting text belong to the panel
 * for the length of the drag, or the first pull sweeps a selection across the rows.
 */
:host([resizing]) { cursor: col-resize; user-select: none; -webkit-user-select: none; }
`;

class ReolinkStaminaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
    this._store = null;
    this._render = frameDebounce(() => this._doRender());
    /** The panel's own box, which decides between the split and the overlay. */
    this._panelWidth = 0;
    this._panelHeight = 0;
    this._layoutSplit = false;
    /** The width the divider was dragged to, or null for the opening one. */
    this._playerWidth = readPlayerWidth();
    /** And the width that is actually applied, which is that one held inside the bounds. */
    this._appliedWidth = 0;
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
    this._watchSize();
  }

  disconnectedCallback() {
    this.removeEventListener("keydown", this._onKeydown);
    this._sizeObserver?.disconnect();
    this._sizeObserver = null;
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
    this._maybeIntroduce();
  }

  /**
   * Say what the panel can do, once per release.
   *
   * After the first render rather than before it: an introduction over a blank panel is a
   * dialog about nothing, and the list behind it is most of what the words refer to.
   */
  _maybeIntroduce() {
    const version = this._panel?.config?.version;
    if (!shouldIntroduce(this._store.seenVersion, version, this._store.returning)) {
      // Nothing to say, but remember where this browser got to — otherwise the *next*
      // release would look like a first install and stay silent for ever.
      this._store.markIntroduced(version);
      return;
    }
    this._whatsNew.open(version);
  }

  /**
   * Go to this integration's own page in Home Assistant.
   *
   * Pushing the path and announcing it, which is how Home Assistant's router is told to
   * follow a link from inside a panel: it listens for `location-changed` rather than
   * watching the history. A plain assignment would work too and would reload the whole
   * frontend to get somewhere two clicks away.
   */
  _openSettings() {
    const path = "/config/integrations/integration/reolink_stamina";
    history.pushState(null, "", path);
    window.dispatchEvent(
      new CustomEvent("location-changed", { bubbles: true, composed: true, detail: { replace: false } })
    );
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
      el("div", { class: "app-header__title", text: "Reolink Stamina" }),
      el("div", { class: "spacer" }),
      // Beside the name of the thing it introduces, which is where somebody looks for
      // "what is this". A sparkle rather than a question mark: this is what the panel can
      // do, not a help desk.
      el(
        "button",
        {
          class: "icon-btn",
          title: "Settings",
          "aria-label": "Reolink Stamina settings",
          onclick: () => this._openSettings(),
        },
        icon("mdi:cog-outline")
      ),
      el(
        "button",
        {
          class: "icon-btn",
          title: "What this panel can do",
          "aria-label": "What this panel can do",
          onclick: () => this._whatsNew?.open(this._panel?.config?.version),
        },
        icon("mdi:star-four-points-outline")
      )
    );

    // Setup view
    this._picker = el("reolink-device-picker", { class: "scroll", style: { overflow: "auto", flex: "1" } });

    // Main view
    this._toolbar = el("reolink-stamina-toolbar");
    this._list = el("reolink-event-list", { class: "pane-list scroll" });
    this._player = el("reolink-event-player");
    this._playerPane = el("div", { class: "pane-player", hidden: true }, this._player);

    // The divider between them. A separator rather than a decoration, so it can be moved from
    // the keyboard as well as dragged, and a double-click puts it back where it started.
    this._gutter = el("div", {
      class: "gutter",
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize the player",
      tabindex: "0",
      hidden: true,
      onpointerdown: (event) => this._onGutterDown(event),
      onkeydown: (event) => this._onGutterKey(event),
      ondblclick: () => this._setPlayerWidth(null),
    });

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

    this._split = el("div", { class: "split" }, this._list, this._gutter, this._playerPane);
    // Built with the panel but only ever shown deliberately: it costs one element and one
    // listener, and lazily creating it would mean the toolbar's way back to it could open
    // nothing on a slow first click.
    this._whatsNew = el("reolink-stamina-whats-new");
    this._whatsNew.addEventListener("seen", () => {
      this._store?.markIntroduced(this._panel?.config?.version);
    });

    this._main = el("div", { class: "content" }, this._toolbar, this._split, this._whatsNew);

    this._body = el("div", { class: "content" });
    this.shadowRoot.append(header, this._body);
    this._built = true;
    this._syncMenuButton();
    // The panel may well have been measured before there was anything to lay out.
    this._applyLayout();
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

  // ------------------------------------------------------------------ the divider

  /**
   * Follow the panel's own size, which is what decides the layout.
   *
   * Its box rather than the window's: the sidebar is docked outside the panel, so the two
   * differ by however wide it is, and a split is only worth opening if the panel itself has
   * the room. Measuring the panel and not the split is also what keeps a drag from feeding
   * back into this — dragging the divider changes the panes' widths and never the panel's.
   */
  _watchSize() {
    if (this._sizeObserver || typeof ResizeObserver === "undefined") return;
    this._sizeObserver = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1].contentRect;
      this._panelWidth = box.width;
      this._panelHeight = box.height;
      this._applyLayout();
    });
    this._sizeObserver.observe(this);
  }

  /** Side by side, or the player over the list — and if side by side, how wide. */
  _applyLayout() {
    this._layoutSplit = splitFits(this._panelWidth, this._panelHeight);
    this.dataset.layout = this._layoutSplit ? "split" : "overlay";
    if (!this._built) return;
    this._syncGutter();
    if (this._layoutSplit) this._applyPlayerWidth();
  }

  /** The divider is only there when there are two panes for it to be between. */
  _syncGutter() {
    this._gutter.hidden = !this._layoutSplit || this._playerPane.hidden;
  }

  /** The space the two panes share: the panel, less the divider between them. */
  get _sharedWidth() {
    return Math.max(0, this._panelWidth - GUTTER);
  }

  /**
   * Put the current width on the split, held inside what the panel can give.
   *
   * The dragged width is kept as it was asked for rather than overwritten by the clamp, so
   * that narrowing the window and widening it again gives the picture its size back instead
   * of leaving it at whatever the narrow window could spare.
   */
  _applyPlayerWidth() {
    const total = this._sharedWidth;
    const width = clampPlayerWidth(this._playerWidth ?? openingPlayerWidth(total), total);
    this._appliedWidth = width;
    this._split.style.setProperty("--rv-player-width", `${width}px`);
    this._gutter.setAttribute("aria-valuenow", String(width));
    this._gutter.setAttribute("aria-valuemin", String(PLAYER_MIN));
    this._gutter.setAttribute("aria-valuemax", String(Math.max(PLAYER_MIN, total - LIST_MIN)));
  }

  /**
   * Move the divider. `null` puts the player back to the width it opens at.
   *
   * `remember` is off for the frames of a drag: the width worth writing down is the one it
   * was let go at, not the couple of hundred it passed through on the way.
   */
  _setPlayerWidth(width, { remember = true } = {}) {
    this._playerWidth = width === null ? null : clampPlayerWidth(width, this._sharedWidth);
    if (remember) savePlayerWidth(this._playerWidth);
    this._applyPlayerWidth();
  }

  /**
   * Drag the divider.
   *
   * The pointer is captured, so the rest of the drag is reported here however far it strays
   * over the list or the video — and dragging left widens the player, which is the pane on
   * the right. Focus follows the grab so the arrow keys can finish the job; `:focus-visible`
   * means that costs a mouse user no focus ring.
   */
  _onGutterDown(event) {
    if (event.button > 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = this._appliedWidth;
    this._gutter.setPointerCapture(event.pointerId);
    this._gutter.focus({ preventScroll: true });
    this._gutter.toggleAttribute("dragging", true);
    this.toggleAttribute("resizing", true);

    const move = (moved) => {
      this._setPlayerWidth(startWidth - (moved.clientX - startX), { remember: false });
    };
    const done = () => {
      this._gutter.removeEventListener("pointermove", move);
      this._gutter.removeEventListener("pointerup", done);
      this._gutter.removeEventListener("pointercancel", done);
      this._gutter.removeAttribute("dragging");
      this.removeAttribute("resizing");
      savePlayerWidth(this._playerWidth);
    };
    this._gutter.addEventListener("pointermove", move);
    this._gutter.addEventListener("pointerup", done);
    this._gutter.addEventListener("pointercancel", done);
  }

  /** And move it from the keyboard, the way a separator is expected to. */
  _onGutterKey(event) {
    const step = event.shiftKey ? 64 : 16;
    let width;
    if (event.key === "ArrowLeft") width = this._appliedWidth + step;
    else if (event.key === "ArrowRight") width = this._appliedWidth - step;
    else if (event.key === "Home") width = PLAYER_MIN;
    else if (event.key === "End") width = this._sharedWidth; // clamped down to the ceiling
    else return;
    event.preventDefault();
    this._setPlayerWidth(width);
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
    this._syncGutter();
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
