/**
 * Device and camera selection.
 *
 * Everything here comes from the official Reolink integration. Devices that exist but are
 * not usable are shown with the reason rather than hidden, so a missing recorder is
 * never a silent mystery. Entries the user disabled in Home Assistant are the exception:
 * those are not sent here at all, because a deliberate choice is not a fault to report.
 */

import { adoptStyles, el, icon, reconcile } from "../dom.js";
import { SHARED } from "../theme.js";

/**
 * What a device is, when it is not a recorder.
 *
 * Everything else here has been tested against a recorder, so a hub or a camera is worth
 * labelling as the unknown quantity it is.
 */
const KIND_TEXT = {
  hub: { label: "Home Hub", hint: "Hubs record to their own storage. Untested here — reports welcome." },
  camera: {
    label: "Camera",
    hint: "A standalone camera recording to its own SD card. Untested here — reports welcome.",
  },
};

const STATUS_TEXT = {
  not_connected: { label: "Not connected", hint: "Home Assistant cannot reach this device right now." },
  setup_error: { label: "Setup failed", hint: "Check the Reolink integration for this device." },
  // Entries the user disabled are not sent here at all -- disabling one is a decision,
  // not a fault. This is the entry that is set up and enabled but is not running.
  not_loaded: {
    label: "Not loaded",
    hint: "This Reolink entry is not running. Check it in the Reolink integration.",
  },
  incompatible: {
    label: "Unsupported",
    hint: "This panel could not read the Reolink integration for this device.",
  },
};

const STYLES = /* css */ `
:host { display: block; }

.wrap {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px 20px 96px;
}

.intro { margin-bottom: 24px; }
.intro p { margin: 8px 0 0; color: var(--rv-text-dim); line-height: 1.55; max-width: 68ch; }

.grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
}

.device { padding: 0; overflow: hidden; }
.device--off { opacity: 0.62; }

.device__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--rv-line);
}
.device__head--plain { border-bottom: none; }

.device__title { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.device__meta { font-size: 0.78rem; color: var(--rv-text-dim); }

.device__toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: var(--rv-radius-pill);
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--rv-text-dim);
}
.device__toggle:hover { background: color-mix(in srgb, var(--rv-text) 8%, transparent); }

.cams { display: flex; flex-direction: column; padding: 6px; }

.cam {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 9px 10px;
  border-radius: var(--rv-radius-sm);
  text-align: left;
  transition: background 120ms var(--rv-ease);
}
.cam:hover:not(:disabled) { background: color-mix(in srgb, var(--rv-accent) 9%, transparent); }
.cam__body { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.cam__name { font-size: 0.92rem; font-weight: 500; }
.cam__ai { display: flex; gap: 5px; flex-wrap: wrap; }

/* The pick target and the "what has this learned" button sit side by side, and the row is
   the flex container rather than the button — a button inside a button is not markup a
   browser will keep. */
.cam-row { display: flex; align-items: center; gap: 2px; }
.cam-row .cam { flex: 1; min-width: 0; }
.learned {
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; padding: 6px; border-radius: 50%;
  border: 1px solid transparent; background: none; cursor: pointer;
  color: var(--rv-text-dim);
  transition: color 120ms var(--rv-ease), background 120ms var(--rv-ease);
}
.learned:hover { color: var(--rv-text); background: color-mix(in srgb, var(--rv-text) 10%, transparent); }
.learned .icon { --mdc-icon-size: 17px; width: 17px; height: 17px; display: block; }
.cam__ai span {
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--rv-text-dim);
}
.cam--disabled { opacity: 0.5; }

.check {
  width: 20px;
  height: 20px;
  border-radius: 6px;
  border: 2px solid color-mix(in srgb, var(--rv-text) 32%, transparent);
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  transition: background 120ms var(--rv-ease), border-color 120ms var(--rv-ease);
}
.check[data-state="on"], .check[data-state="mixed"] {
  background: var(--rv-accent);
  border-color: var(--rv-accent);
  color: var(--rv-text-on-accent);
}
.check .icon { --mdc-icon-size: 15px; width: 15px; height: 15px; }
.check[data-state="off"] .icon { display: none; }

.warn {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin: 0 16px 14px;
  padding: 10px 12px;
  border-radius: var(--rv-radius-sm);
  background: color-mix(in srgb, var(--rv-warn) 14%, transparent);
  font-size: 0.82rem;
  line-height: 1.45;
}
.warn .icon { color: var(--rv-warn); }

.status-hint { padding: 0 16px 16px; font-size: 0.83rem; color: var(--rv-text-dim); line-height: 1.45; }

.footer {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 24px;
  padding: 14px 4px;
  background: linear-gradient(to top, var(--rv-bg) 60%, transparent);
}
`;

export class DevicePicker extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
  }

  set store(store) {
    this._store = store;
    this.render();
  }

  get store() {
    return this._store;
  }

  render() {
    if (!this._store) return;
    if (!this._built) this._build();
    this._update();
  }

  _build() {
    const root = el("div", { class: "wrap" });

    this._intro = el("div", { class: "intro" });
    this._grid = el("div", { class: "grid" });
    this._emptyHost = el("div");

    this._count = el("div", { class: "small dim" });
    this._continue = el(
      "button",
      {
        class: "btn btn--primary",
        onclick: () => this._store.completeSetup(),
      },
      "Review events",
      icon("mdi:arrow-right")
    );

    this._footer = el(
      "div",
      { class: "footer" },
      this._count,
      el("div", { class: "spacer" }),
      this._continue
    );

    root.append(this._intro, this._emptyHost, this._grid, this._footer);
    this.shadowRoot.append(root);
    this._built = true;
  }

  _update() {
    const store = this._store;

    this._intro.replaceChildren(
      el("h1", { class: "h1", text: "Choose what to review" }),
      el("p", {
        text: "These are the Reolink devices found through the Reolink integration — recorders, hubs and standalone cameras. Pick the cameras you want in your event timeline; you can change this at any time.",
      })
    );

    // Empty and error states
    this._emptyHost.replaceChildren();
    if (store.loadingDevices) {
      this._grid.replaceChildren(
        ...[0, 1].map(() => el("div", { class: "card skeleton", style: { height: "220px" } }))
      );
      this._footer.style.display = "none";
      return;
    }

    if (store.deviceError) {
      this._emptyHost.append(
        el(
          "div",
          { class: "empty" },
          icon("mdi:alert-circle-outline"),
          el("div", { class: "empty__title", text: "Could not load your devices" }),
          el("div", { class: "empty__body", text: store.deviceError })
        )
      );
      this._grid.replaceChildren();
      this._footer.style.display = "none";
      return;
    }

    if (store.devices.length === 0) {
      this._emptyHost.append(
        el(
          "div",
          { class: "empty" },
          icon("mdi:nas"),
          el("div", { class: "empty__title", text: "No Reolink NVR found" }),
          el("div", {
            class: "empty__body",
            text: "Set up a Reolink device in the official Reolink integration and it will appear here — Home Hubs and standalone cameras as well as recorders.",
          })
        )
      );
      this._grid.replaceChildren();
      this._footer.style.display = "none";
      return;
    }

    this._footer.style.display = "";
    reconcile(
      this._grid,
      store.devices,
      (device) => device.entry_id,
      (device) => this._createCard(device),
      (node, device) => this._updateCard(node, device)
    );

    const selected = store.cameras.length;
    this._count.textContent =
      selected === 0
        ? "No cameras selected"
        : `${selected} camera${selected === 1 ? "" : "s"} selected`;
    this._continue.disabled = selected === 0;
  }

  _createCard(device) {
    const refs = {};
    const card = el("section", { class: "card device" });

    refs.title = el("div", { class: "h2 truncate" });
    refs.meta = el("div", { class: "device__meta" });
    refs.badge = el("span");
    refs.kind = el("span");
    refs.toggle = el("button", {
      class: "device__toggle",
      onclick: () => this._store.toggleDevice(device.entry_id),
    });
    refs.check = el("span", { class: "check" }, icon("mdi:check", "icon--sm"));
    refs.toggle.append(refs.check, el("span", { text: "All" }));

    refs.head = el(
      "header",
      { class: "device__head" },
      el("div", { class: "device__title" }, refs.title, refs.meta, refs.kind, refs.badge),
      refs.toggle
    );

    refs.warn = el("div");
    refs.cams = el("div", { class: "cams" });
    refs.hint = el("div", { class: "status-hint" });

    card.append(refs.head, refs.warn, refs.cams, refs.hint);
    card.__refs = refs;
    return card;
  }

  _updateCard(card, device) {
    const refs = card.__refs;
    const store = this._store;
    const usable = device.status === "ok";

    card.classList.toggle("device--off", !usable);
    refs.title.textContent = device.name;

    const meta = [device.model, device.sw_version].filter(Boolean).join(" · ");
    refs.meta.textContent = meta;
    refs.meta.style.display = meta ? "" : "none";

    // What it is, when it is not the recorder this panel was built for.
    const kind = KIND_TEXT[device.kind];
    refs.kind.replaceChildren();
    refs.kind.style.display = kind ? "" : "none";
    if (kind) {
      const badge = el("span", { class: "badge", style: { marginTop: "4px" } }, kind.label);
      badge.title = kind.hint;
      refs.kind.append(badge);
    }

    // Status
    refs.badge.replaceChildren();
    if (!usable) {
      const status = STATUS_TEXT[device.status] || STATUS_TEXT.not_loaded;
      refs.badge.append(
        el("span", { class: "badge badge--warn", style: { marginTop: "4px" } }, status.label)
      );
      refs.hint.textContent = status.hint;
      refs.hint.style.display = "";
    } else {
      refs.hint.textContent = "";
      refs.hint.style.display = "none";
    }

    refs.toggle.style.display = usable ? "" : "none";
    refs.head.classList.toggle("device__head--plain", !usable);

    // Warnings that affect what the user can expect to find
    refs.warn.replaceChildren();
    if (usable && !device.has_storage) {
      refs.warn.append(
        el(
          "div",
          { class: "warn" },
          icon("mdi:harddisk-remove"),
          el("span", {
            text:
              device.kind === "nvr"
                ? "No storage detected on this recorder. Without a working HDD there are no recordings to review."
                : "No storage detected on this device. Without a working SD card there are no recordings to review.",
          })
        )
      );
    }
    if (usable && !device.reports_triggers) {
      refs.warn.append(
        el(
          "div",
          { class: "warn" },
          icon("mdi:tag-off-outline"),
          el("span", {
            text:
              "This device does not report event types, so recordings will be listed without person, vehicle or animal labels.",
          })
        )
      );
    }

    // Select-all state
    const state = store.isDeviceFullySelected(device.entry_id)
      ? "on"
      : store.isDevicePartiallySelected(device.entry_id)
        ? "mixed"
        : "off";
    refs.check.dataset.state = state;
    refs.toggle.setAttribute("aria-pressed", state === "on" ? "true" : "false");

    // Cameras
    if (!usable) {
      refs.cams.replaceChildren();
      return;
    }

    reconcile(
      refs.cams,
      device.cameras,
      (camera) => camera.channel,
      (camera) => this._createCameraRow(device, camera),
      (node, camera) => this._updateCameraRow(node, device, camera)
    );
  }

  _createCameraRow(device, camera) {
    const refs = {};
    refs.check = el("span", { class: "check" }, icon("mdi:check", "icon--sm"));
    refs.name = el("span", { class: "cam__name truncate" });
    refs.ai = el("div", { class: "cam__ai" });

    refs.pick = el(
      "button",
      {
        class: "cam",
        onclick: () => {
          if (camera.can_playback) this._store.toggleCamera(device.entry_id, camera.channel);
        },
      },
      refs.check,
      el("div", { class: "cam__body" }, refs.name, refs.ai)
    );

    // Only present once the model is running: with nothing learned to show, a button that
    // opens an explanation of why it cannot do anything is worse than no button.
    refs.learned = el(
      "button",
      {
        class: "learned",
        title: "What this camera has learned",
        "aria-label": "What this camera has learned",
        onclick: (event) => {
          event.currentTarget.blur();
          this.dispatchEvent(
            new CustomEvent("show-learned", {
              bubbles: true,
              composed: true,
              detail: { device, camera },
            })
          );
        },
      },
      icon("mdi:chart-box-outline")
    );

    const row = el("div", { class: "cam-row" }, refs.pick, refs.learned);
    row.__refs = refs;
    return row;
  }

  _updateCameraRow(row, device, camera) {
    const refs = row.__refs;
    const selected = this._store.isCameraSelected(device.entry_id, camera.channel);

    refs.name.textContent = camera.name;
    refs.check.dataset.state = selected ? "on" : "off";
    refs.pick.setAttribute("aria-pressed", selected ? "true" : "false");
    refs.pick.disabled = !camera.can_playback;
    refs.pick.classList.toggle("cam--disabled", !camera.can_playback);
    refs.learned.hidden = !this._store.relevanceEnabled || !camera.can_playback;

    refs.ai.replaceChildren();
    if (!camera.can_playback) {
      refs.ai.append(el("span", { text: "Playback not supported" }));
    } else if (camera.ai_types?.length) {
      refs.ai.append(...camera.ai_types.map((type) => el("span", { text: type })));
    }
  }
}

// Guarded: Home Assistant may import this module more than once (after an
// update, or from a cached copy), and a duplicate define() throws and takes the
// whole panel down with it.
if (!customElements.get("reolink-device-picker")) customElements.define("reolink-device-picker", DevicePicker);
