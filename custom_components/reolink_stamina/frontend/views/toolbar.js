/**
 * Date navigation, trigger filters, camera selection and freshness feedback.
 *
 * The freshness pill is the visible half of the stale-while-revalidate contract: the
 * list always shows something immediately, and this is where the panel admits how old
 * that something is and whether a refresh is running.
 */

import { adoptStyles, el, icon, reconcile } from "../dom.js";
import { SHARED } from "../theme.js";
import { RANGE_PRESETS } from "../store.js";
import {
  FILTER_GROUPS,
  addDays,
  formatAge,
  formatDayLabel,
  isoDate,
  parseIsoDate,
} from "../format.js";

const STYLES = /* css */ `
:host { display: block; position: relative; z-index: 3; }

.bar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
  background: var(--rv-surface);
  border-bottom: 1px solid var(--rv-line);
  transition: gap 260ms var(--rv-ease);
}

.line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/*
 * The wrappers that fold away, and the mechanism that folds them.
 *
 * A grid row animated between 1fr and 0fr, rather than a max-height guess: the row is
 * exactly as tall as whatever is in it, so the fold lands on the real height whether the
 * trigger chips sit on one line or wrap onto two. A display of none cannot be animated at
 * all, which is why the toolbar used to snap in and out.
 */
.collapse {
  display: grid;
  grid-template-rows: 1fr;
  transition:
    grid-template-rows 260ms var(--rv-ease),
    opacity 180ms var(--rv-ease),
    margin-top 260ms var(--rv-ease);
}
.collapse__clip { min-height: 0; overflow: hidden; }

.date {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: var(--rv-radius-pill);
  background: var(--rv-surface-sunken);
  font-size: 0.92rem;
  font-weight: 600;
}
.date:hover { background: color-mix(in srgb, var(--rv-accent) 12%, var(--rv-surface-sunken)); }

.presets { display: flex; gap: 4px; }
.preset {
  padding: 6px 12px;
  border-radius: var(--rv-radius-pill);
  font-size: 0.83rem;
  font-weight: 500;
  color: var(--rv-text-dim);
}
.preset:hover { background: color-mix(in srgb, var(--rv-text) 8%, transparent); }
.preset[aria-pressed="true"] {
  background: color-mix(in srgb, var(--rv-accent) 16%, transparent);
  color: color-mix(in srgb, var(--rv-accent) 85%, var(--rv-text));
}

.fresh {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 0.78rem;
  color: var(--rv-text-dim);
  padding: 4px 10px;
  border-radius: var(--rv-radius-pill);
  background: color-mix(in srgb, var(--rv-text) 6%, transparent);
  white-space: nowrap;
}

.hidden-note { font-size: 0.78rem; color: var(--rv-text-dim); }
.hidden-note button { text-decoration: underline; font-size: inherit; color: inherit; }

/* --------------------------------------------------------------- popovers */

.pop {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  /* The popover hangs down over the row of filter chips below it, which would otherwise
     paint on top: it opens from the first row of the toolbar, and the chips are in the
     second. Stated explicitly rather than relying on paint order. */
  z-index: 40;
  min-width: 260px;
  padding: 12px;
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  box-shadow: var(--rv-shadow-lifted);
  border: 1px solid var(--rv-line);
  animation: rv-enter 140ms var(--rv-ease) both;
}
.pop[hidden] { display: none; }
.pop--right { left: auto; right: 0; }
.anchor { position: relative; }
/* An open popover lifts its anchor too, so nothing in a following row can cover it
   whatever stacking context that row happens to create. */
.anchor:has(.pop:not([hidden])) { z-index: 40; }

/* ---------------------------------------------------------------- calendar */

.cal__head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.cal__month { font-size: 0.92rem; font-weight: 600; }
.cal__grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
.cal__dow {
  text-align: center;
  font-size: 0.68rem;
  font-weight: 700;
  color: var(--rv-text-dim);
  padding-bottom: 4px;
  text-transform: uppercase;
}
.cal__day {
  position: relative;
  height: 36px;
  border-radius: 9px;
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
  display: grid;
  place-items: center;
}
.cal__day:hover:not(:disabled) { background: color-mix(in srgb, var(--rv-accent) 14%, transparent); }
.cal__day:disabled { opacity: 0.25; }
.cal__day[data-selected="true"] {
  background: var(--rv-accent);
  color: var(--rv-text-on-accent);
  font-weight: 700;
}
.cal__day[data-has="true"]::after {
  content: "";
  position: absolute;
  bottom: 5px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--rv-accent);
}
.cal__day[data-selected="true"][data-has="true"]::after { background: var(--rv-text-on-accent); }
.cal__legend { margin-top: 10px; font-size: 0.72rem; color: var(--rv-text-dim); display: flex; align-items: center; gap: 6px; }
.cal__dot { width: 4px; height: 4px; border-radius: 50%; background: var(--rv-accent); }

/* ----------------------------------------------------------------- cameras */

.cams { max-height: 320px; }
.cams__group { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--rv-text-dim); padding: 10px 6px 4px; }
.cam {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 7px 8px;
  border-radius: var(--rv-radius-sm);
  text-align: left;
  font-size: 0.88rem;
}
.cam:hover { background: color-mix(in srgb, var(--rv-accent) 10%, transparent); }
.check {
  width: 18px; height: 18px;
  border-radius: 5px;
  border: 2px solid color-mix(in srgb, var(--rv-text) 32%, transparent);
  display: grid; place-items: center; flex: 0 0 auto;
}
.check[data-state="on"] { background: var(--rv-accent); border-color: var(--rv-accent); color: var(--rv-text-on-accent); }
.check[data-state="off"] .icon { display: none; }
.check .icon { --mdc-icon-size: 13px; width: 13px; height: 13px; }
.pop__foot { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--rv-line); display: flex; }

@media (max-width: 700px) {
  .bar { padding: 10px 12px; }
  .collapse--presets { order: 3; width: 100%; }
  /* Scrolled sideways rather than wrapped, and without a bar of its own: a scrollbar that
     appears across the toolbar of a panel with nothing else to scroll reads as a fault. */
  .presets { overflow-x: auto; scrollbar-width: none; }
  .presets::-webkit-scrollbar { display: none; }
}

/*
 * Folded away while the list is being read — the panel sets the collapsed attribute on the
 * way down and clears it on the way up.
 *
 * The date, the freshness pill and the camera picker stay: they say where you are and are one
 * tap each. What goes is the two tall rows, the range presets and the trigger chips, which
 * together are most of the toolbar's height and none of its context.
 *
 * A wider breakpoint than the layout tweaks above, on purpose: a phone in landscape is short
 * rather than narrow, and that is exactly when a toolbar this tall costs the most.
 */
@media (max-width: 900px) {
  :host([collapsed]) .collapse {
    grid-template-rows: 0fr;
    opacity: 0;
    /* A row of no height is still a row, and a chip in it is still tappable. */
    pointer-events: none;
  }
  /* Both parents go on reserving their gap around a row that now has no height, which
     would leave a band of empty toolbar exactly where the toolbar was folded away. */
  :host([collapsed]) .bar { gap: 0; }
}
@media (max-width: 700px) {
  :host([collapsed]) .collapse--presets { margin-top: -8px; }
}
`;

const DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

export class StaminaToolbar extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
    this._calOpen = false;
    this._camsOpen = false;
    this._calCursor = null;

    this._onDocClick = (event) => {
      if (!this._calOpen && !this._camsOpen) return;
      if (event.composedPath().includes(this)) return;
      this._closePopovers();
    };
    this._onKeydown = (event) => {
      if (event.key === "Escape") this._closePopovers();
    };
  }

  connectedCallback() {
    document.addEventListener("click", this._onDocClick);
    document.addEventListener("keydown", this._onKeydown);
  }

  disconnectedCallback() {
    document.removeEventListener("click", this._onDocClick);
    document.removeEventListener("keydown", this._onKeydown);
  }

  set store(store) {
    this._store = store;
    this.render();
  }

  _closePopovers() {
    this._calOpen = false;
    this._camsOpen = false;
    this.render();
  }

  render() {
    if (!this._store) return;
    if (!this._built) this._build();
    this._update();
  }

  _build() {
    const store = this._store;

    // --- date navigation
    this._prev = el(
      "button",
      { class: "icon-btn", title: "Previous", onclick: () => store.shiftDays(-1) },
      icon("mdi:chevron-left")
    );
    this._next = el(
      "button",
      { class: "icon-btn", title: "Next", onclick: () => store.shiftDays(1) },
      icon("mdi:chevron-right")
    );
    this._dateLabel = el("span");
    this._dateBtn = el(
      "button",
      {
        class: "date",
        onclick: () => {
          this._calOpen = !this._calOpen;
          this._camsOpen = false;
          this._calCursor = parseIsoDate(this._store.endDate);
          this.render();
        },
      },
      icon("mdi:calendar-month-outline"),
      this._dateLabel,
      icon("mdi:menu-down")
    );

    this._calPop = el("div", { class: "pop", hidden: true });
    const dateAnchor = el("div", { class: "anchor" }, this._dateBtn, this._calPop);

    // --- presets
    this._presets = el(
      "div",
      { class: "presets" },
      ...RANGE_PRESETS.map((preset) =>
        el("button", {
          class: "preset",
          text: preset.label,
          dataset: { preset: preset.id },
          onclick: () => store.setRangePreset(preset.id),
        })
      )
    );
    const presetsWrap = el(
      "div",
      { class: "collapse collapse--presets" },
      el("div", { class: "collapse__clip" }, this._presets)
    );

    // --- freshness + actions
    this._fresh = el("div", { class: "fresh" });
    this._refresh = el(
      "button",
      { class: "icon-btn", title: "Refresh from the device", onclick: () => store.refresh() },
      icon("mdi:refresh")
    );

    this._camsLabel = el("span");
    this._camsBtn = el(
      "button",
      {
        class: "btn btn--quiet",
        onclick: () => {
          this._camsOpen = !this._camsOpen;
          this._calOpen = false;
          this.render();
        },
      },
      icon("mdi:cctv"),
      this._camsLabel,
      icon("mdi:menu-down")
    );
    this._camsPop = el("div", { class: "pop pop--right", hidden: true });
    const camsAnchor = el("div", { class: "anchor" }, this._camsBtn, this._camsPop);

    const line1 = el(
      "div",
      { class: "line" },
      this._prev,
      dateAnchor,
      this._next,
      presetsWrap,
      el("div", { class: "spacer" }),
      this._fresh,
      this._refresh,
      camsAnchor
    );

    // --- filters
    this._filters = el("div", { class: "line row--tight" });
    this._unusualHost = el("span", { class: "row--tight" });
    this._hiddenNote = el("div", { class: "hidden-note" });
    const line2 = el(
      "div",
      { class: "collapse collapse--filters" },
      el(
        "div",
        { class: "collapse__clip" },
        el(
          "div",
          { class: "line line--filters" },
          this._filters,
          this._unusualHost,
          el("div", { class: "spacer" }),
          this._hiddenNote
        )
      )
    );

    this.shadowRoot.append(el("div", { class: "bar" }, line1, line2));
    this._built = true;
  }

  _update() {
    const store = this._store;

    // Date label
    this._dateLabel.textContent = store.isSingleDay
      ? formatDayLabel(store.startDate, this._locale)
      : `${formatDayLabel(store.startDate, this._locale)} – ${formatDayLabel(store.endDate, this._locale)}`;
    this._prev.disabled = !store.canShiftBack;
    this._next.disabled = !store.canShiftForward;

    for (const button of this._presets.children) {
      button.setAttribute(
        "aria-pressed",
        button.dataset.preset === store.rangePreset ? "true" : "false"
      );
    }

    // Freshness: the honest status of what is on screen.
    this._fresh.replaceChildren();
    if (store.isUpdating) {
      this._fresh.append(el("div", { class: "spinner" }), el("span", { text: "Updating…" }));
    } else if (store.errors.length > 0) {
      this._fresh.append(
        icon("mdi:cloud-off-outline", "icon--sm"),
        el("span", { text: "Showing cached data" })
      );
      this._fresh.title = store.errors.join("\n");
    } else {
      const age = formatAge(store.oldestAge);
      if (age) {
        this._fresh.append(
          icon("mdi:check-circle-outline", "icon--sm"),
          el("span", { text: `Updated ${age}` })
        );
        this._fresh.title = "";
      }
    }

    // Cameras button
    const count = store.cameras.length;
    this._camsLabel.textContent =
      count === 1 ? store.cameraLabel(store.cameras[0].entry_id, store.cameras[0].channel) : `${count} cameras`;

    // Filters
    // The unlabelled group is only meaningful when there is unlabelled footage to filter:
    // when the integration is configured to fetch it, or when an event-recording camera
    // sent some anyway. Offered whenever the filter is on as well, so it can always be
    // turned back off — without that, rows the user could see could not be dismissed.
    const groups = FILTER_GROUPS.filter(
      (group) =>
        group.id !== "unclassified" ||
        store.options.include_unlabelled ||
        store.filters.has("unclassified") ||
        store.hasUnclassifiedEvents
    );

    reconcile(
      this._filters,
      groups,
      (group) => group.id,
      (group) =>
        el(
          "button",
          {
            class: "chip chip--button",
            dataset: { tone: group.id === "timer" || group.id === "other" ? "neutral" : group.id },
            onclick: () => store.toggleFilter(group.id),
          },
          icon(group.icon),
          el("span", { text: group.label })
        ),
      (node, group) => {
        node.setAttribute("aria-pressed", store.filters.has(group.id) ? "true" : "false");
      }
    );

    // "Unusual" is not another kind of thing to show, so it is not one of the trigger chips:
    // it narrows whatever they left. Appended after them, and only once a camera in the
    // selection has enough behind it to have an opinion — a chip that can only ever return
    // nothing is worse than no chip.
    this._unusualHost.replaceChildren();
    if (store.relevanceEnabled) {
      const ready = [...store.relevance.values()].some((item) => item.state === "active");
      const chip = el(
        "button",
        {
          class: "chip chip--button",
          dataset: { tone: "alert" },
          disabled: !ready,
          title: ready
            ? "Show only what is unusual for these cameras"
            : "Still learning what is normal on these cameras",
          onclick: () => store.toggleUnusualOnly(),
        },
        icon("mdi:circle-slice-8"),
        el("span", { text: ready ? "Unusual" : "Learning…" })
      );
      chip.setAttribute("aria-pressed", store.unusualOnly ? "true" : "false");
      this._unusualHost.append(chip);
    }

    const total = store.totalEventCount;
    const shown = store.events.length;
    this._hiddenNote.replaceChildren();
    if (total > shown) {
      this._hiddenNote.append(
        el("span", { text: `${total - shown} hidden by filters · ` }),
        el("button", { text: "show all", onclick: () => store.setAllFilters(true) })
      );
    }

    // Popovers
    this._calPop.hidden = !this._calOpen;
    this._camsPop.hidden = !this._camsOpen;
    if (this._calOpen) this._renderCalendar();
    if (this._camsOpen) this._renderCameras();
  }

  // ---------------------------------------------------------------- calendar

  _renderCalendar() {
    const store = this._store;
    const cursor = this._calCursor || parseIsoDate(store.endDate);
    const year = cursor.getFullYear();
    const month = cursor.getMonth();

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const earliest = addDays(today, -store.searchWindowDays);
    const withRecordings = store.daysWithRecordings;
    const sameMonthAsData =
      store.calendarMonth === `${year}-${month + 1}`;

    const first = new Date(year, month, 1);
    // Monday-first grid
    const lead = (first.getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    const grid = el("div", { class: "cal__grid" });
    for (const label of DOW) grid.append(el("div", { class: "cal__dow", text: label }));
    for (let index = 0; index < lead; index += 1) grid.append(el("div"));

    for (let day = 1; day <= daysInMonth; day += 1) {
      const date = new Date(year, month, day);
      const iso = isoDate(date);
      const outOfRange = date > today || date < earliest;
      grid.append(
        el("button", {
          class: "cal__day",
          text: String(day),
          disabled: outOfRange,
          dataset: {
            selected: String(iso === store.startDate && store.isSingleDay),
            has: String(sameMonthAsData && withRecordings.has(day)),
          },
          onclick: () => {
            store.setSingleDay(iso);
            this._closePopovers();
          },
        })
      );
    }

    const monthLabel = first.toLocaleDateString(this._locale, { month: "long", year: "numeric" });
    this._calPop.replaceChildren(
      el(
        "div",
        { class: "cal__head" },
        el(
          "button",
          {
            class: "icon-btn",
            title: "Previous month",
            onclick: () => {
              this._calCursor = new Date(year, month - 1, 1);
              this.render();
            },
          },
          icon("mdi:chevron-left")
        ),
        el("div", { class: "cal__month", text: monthLabel }),
        el(
          "button",
          {
            class: "icon-btn",
            title: "Next month",
            disabled: new Date(year, month + 1, 1) > today,
            onclick: () => {
              this._calCursor = new Date(year, month + 1, 1);
              this.render();
            },
          },
          icon("mdi:chevron-right")
        )
      ),
      grid,
      el(
        "div",
        { class: "cal__legend" },
        el("span", { class: "cal__dot" }),
        el("span", {
          text: sameMonthAsData
            ? "days with recordings"
            : "open a day in this month to see which days have recordings",
        })
      )
    );
  }

  // ----------------------------------------------------------------- cameras

  _renderCameras() {
    const store = this._store;
    const content = el("div", { class: "cams scroll" });

    for (const device of store.usableDevices) {
      content.append(el("div", { class: "cams__group", text: device.name }));
      for (const camera of device.cameras) {
        const selected = store.isCameraSelected(device.entry_id, camera.channel);
        content.append(
          el(
            "button",
            {
              class: "cam",
              disabled: !camera.can_playback,
              onclick: () => {
                store.toggleCamera(device.entry_id, camera.channel);
                this.render();
              },
            },
            el(
              "span",
              { class: "check", dataset: { state: selected ? "on" : "off" } },
              icon("mdi:check")
            ),
            el("span", { class: "truncate", text: camera.name })
          )
        );
      }
    }

    this._camsPop.replaceChildren(
      content,
      el(
        "div",
        { class: "pop__foot" },
        el(
          "button",
          {
            class: "btn btn--quiet",
            onclick: () => {
              this._closePopovers();
              store.reopenSetup();
            },
          },
          icon("mdi:tune-variant"),
          "Change devices"
        )
      )
    );
  }
}

// Guarded: Home Assistant may import this module more than once (after an
// update, or from a cached copy), and a duplicate define() throws and takes the
// whole panel down with it.
if (!customElements.get("reolink-stamina-toolbar")) customElements.define("reolink-stamina-toolbar", StaminaToolbar);
