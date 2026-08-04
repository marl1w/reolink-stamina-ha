/**
 * The event timeline.
 *
 * Rows are reconciled by event id rather than rebuilt, so the background patches that
 * arrive as the NVR answers never move the scroll position, drop keyboard focus, or
 * re-run the row animation on rows that were already there.
 */

import { adoptStyles, el, icon, reconcile } from "../dom.js";
import { SHARED } from "../theme.js";
import {
  formatAge,
  formatDayLabel,
  formatDuration,
  formatSize,
  formatTime,
  primaryTrigger,
  sortTriggers,
  streamLabel,
  triggerMeta,
} from "../format.js";

const STYLES = /* css */ `
:host { display: block; height: 100%; }

.list { padding: 8px 12px 40px; }

.day {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 8px 8px;
  background: linear-gradient(to bottom, var(--rv-bg) 65%, transparent);
  backdrop-filter: blur(6px);
}
.day__label { font-size: 0.86rem; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; color: var(--rv-text-dim); }
.day__count { font-size: 0.76rem; color: var(--rv-text-dim); }

.event {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  margin-bottom: 6px;
  padding: 11px 14px;
  text-align: left;
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  box-shadow: var(--rv-shadow);
  border: 1px solid transparent;
  transition: border-color 140ms var(--rv-ease), transform 140ms var(--rv-ease), box-shadow 140ms var(--rv-ease);
}
.event:hover { border-color: color-mix(in srgb, var(--rv-accent) 45%, transparent); }
.event[data-selected="true"] {
  border-color: var(--rv-accent);
  box-shadow: 0 0 0 1px var(--rv-accent), var(--rv-shadow);
}
.event[data-playable="false"] { opacity: 0.7; }

.mark {
  width: 38px;
  height: 38px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  background: color-mix(in srgb, var(--mark-tone, var(--rv-tone-neutral)) 16%, transparent);
  color: color-mix(in srgb, var(--mark-tone, var(--rv-tone-neutral)) 90%, var(--rv-text));
}
.mark[data-tone="person"] { --mark-tone: var(--rv-tone-person); }
.mark[data-tone="vehicle"] { --mark-tone: var(--rv-tone-vehicle); }
.mark[data-tone="animal"] { --mark-tone: var(--rv-tone-animal); }
.mark[data-tone="alert"] { --mark-tone: var(--rv-tone-alert); }
.mark[data-tone="motion"] { --mark-tone: var(--rv-tone-motion); }
.mark[data-tone="neutral"] { --mark-tone: var(--rv-tone-neutral); }
.mark .icon { --mdc-icon-size: 21px; width: 21px; height: 21px; }

.body { display: flex; flex-direction: column; gap: 5px; min-width: 0; flex: 1; }
.headline { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.time { font-size: 1rem; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.camera { font-size: 0.84rem; color: var(--rv-text-dim); }
.chips { display: flex; gap: 5px; flex-wrap: wrap; }

.side { display: flex; align-items: center; gap: 14px; flex: 0 0 auto; }
.facts { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.facts__main { font-size: 0.86rem; font-variant-numeric: tabular-nums; }
.streams { display: flex; gap: 4px; }
.checking { display: flex; align-items: center; gap: 5px; font-size: 0.7rem; color: var(--rv-text-dim); }
.checking .spinner { width: 10px; height: 10px; border-width: 1.5px; }
.go { color: var(--rv-text-dim); }

/* ------------------------------------------------------------------ loading */

.sk-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 6px;
  padding: 11px 14px;
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  box-shadow: var(--rv-shadow);
}
.sk-mark { width: 38px; height: 38px; border-radius: 12px; }
.sk-lines { flex: 1; display: flex; flex-direction: column; gap: 7px; }

.notice {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 8px 0 14px;
  padding: 11px 14px;
  border-radius: var(--rv-radius-sm);
  background: color-mix(in srgb, var(--rv-warn) 13%, transparent);
  font-size: 0.83rem;
  line-height: 1.45;
}
.notice .icon { color: var(--rv-warn); }

@media (max-width: 620px) {
  .list { padding: 6px 10px 32px; }
  .facts__main { display: none; }
  .camera { display: block; }
  .headline { flex-direction: column; align-items: flex-start; gap: 2px; }
}
`;

export class EventList extends HTMLElement {
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

  _build() {
    this._notices = el("div");
    this._list = el("div", {
      class: "list",
      role: "listbox",
      onkeydown: (event) => this._onKeydown(event),
    });
    this._stateHost = el("div");
    this.shadowRoot.append(el("div", {}, this._notices, this._stateHost, this._list));
    this._built = true;
  }

  render() {
    if (!this._store) return;
    if (!this._built) this._build();

    const store = this._store;

    // --- notices that change what the user should expect from the list
    this._notices.replaceChildren();
    if (store.truncated) {
      this._notices.append(
        el(
          "div",
          { class: "notice" },
          icon("mdi:information-outline"),
          el("span", {
            text:
              "This range covers more camera-days than can be searched at once. Narrow the date range or select fewer cameras to see everything.",
          })
        )
      );
    }
    if (store.errors.length > 0) {
      const age = formatAge(store.oldestAge);
      this._notices.append(
        el(
          "div",
          { class: "notice" },
          icon("mdi:cloud-off-outline"),
          el("span", {
            text: `Could not reach the NVR${age ? `, showing results from ${age}` : ""}. ${store.errors[0]}`,
          })
        )
      );
    }

    // --- first load and empty states
    const groups = store.eventsByDay;
    this._stateHost.replaceChildren();

    if (store.cameras.length === 0) {
      this._list.replaceChildren();
      this._stateHost.append(
        this._empty("mdi:cctv-off", "No cameras selected", "Pick at least one camera to build a timeline.")
      );
      return;
    }

    if (groups.length === 0 && store.isFirstLoad) {
      this._list.replaceChildren();
      this._stateHost.append(this._skeleton());
      return;
    }

    const skipped = store.unlabelledSkipped;
    if (skipped > 0) {
      this._notices.append(
        el(
          "div",
          { class: "notice", style: { background: "transparent", paddingLeft: "6px" } },
          icon("mdi:filter-check-outline"),
          el("span", {
            class: "dim",
            text: `${skipped} continuous recording${skipped === 1 ? "" : "s"} without a detection were skipped, so they were never downloaded.`,
          })
        )
      );
    }

    if (groups.length === 0) {
      this._list.replaceChildren();
      const filtered = store.totalEventCount > 0;
      this._stateHost.append(
        filtered
          ? this._empty(
              "mdi:filter-off-outline",
              "Everything is filtered out",
              `${store.totalEventCount} recordings in this range do not match the active filters.`
            )
          : this._empty(
              "mdi:calendar-blank-outline",
              skipped > 0 ? "No detections" : "Nothing recorded",
              store.isUpdating
                ? "Still searching the NVR…"
                : skipped > 0
                  ? `${skipped} continuous recordings were found, but none carried a person, vehicle or animal detection.`
                  : "No recordings were found for these cameras in this period."
            )
      );
      return;
    }

    // --- the list itself, flattened so day headings and rows reconcile together
    const items = [];
    for (const group of groups) {
      items.push({ type: "day", key: `day:${group.day}`, group });
      for (const event of group.events) items.push({ type: "event", key: event.id, event });
    }

    reconcile(
      this._list,
      items,
      (item) => item.key,
      (item) => (item.type === "day" ? this._createDay() : this._createEvent()),
      (node, item) =>
        item.type === "day" ? this._updateDay(node, item.group) : this._updateEvent(node, item.event)
    );
  }

  _empty(iconName, title, body) {
    return el(
      "div",
      { class: "empty enter" },
      icon(iconName),
      el("div", { class: "empty__title", text: title }),
      el("div", { class: "empty__body", text: body })
    );
  }

  _skeleton() {
    return el(
      "div",
      { class: "list" },
      ...Array.from({ length: 5 }, () =>
        el(
          "div",
          { class: "sk-row" },
          el("div", { class: "skeleton sk-mark" }),
          el(
            "div",
            { class: "sk-lines" },
            el("div", { class: "skeleton", style: { width: "38%", height: "13px" } }),
            el("div", { class: "skeleton", style: { width: "22%", height: "11px" } })
          ),
          el("div", { class: "skeleton", style: { width: "54px", height: "13px" } })
        )
      )
    );
  }

  // --------------------------------------------------------------- day heading

  _createDay() {
    const refs = {};
    refs.label = el("div", { class: "day__label" });
    refs.count = el("div", { class: "day__count" });
    const node = el("div", { class: "day", role: "presentation" }, refs.label, refs.count);
    node.__refs = refs;
    return node;
  }

  _updateDay(node, group) {
    node.__refs.label.textContent = formatDayLabel(group.day);
    node.__refs.count.textContent = `${group.events.length} event${group.events.length === 1 ? "" : "s"}`;
  }

  // ----------------------------------------------------------------- event row

  _createEvent() {
    const refs = {};
    refs.mark = el("div", { class: "mark" }, (refs.markIcon = icon("mdi:record-circle-outline")));
    refs.time = el("span", { class: "time" });
    refs.camera = el("span", { class: "camera truncate" });
    refs.chips = el("div", { class: "chips" });
    refs.facts = el("div", { class: "facts" });

    const node = el(
      "button",
      { class: "event enter", role: "option" },
      refs.mark,
      el(
        "div",
        { class: "body" },
        el("div", { class: "headline" }, refs.time, refs.camera),
        refs.chips
      ),
      el("div", { class: "side" }, refs.facts, icon("mdi:play-circle-outline", "go"))
    );
    node.__refs = refs;
    return node;
  }

  _updateEvent(node, event) {
    const refs = node.__refs;
    const store = this._store;

    // Leading mark reflects the most significant trigger.
    const primary = primaryTrigger(event.triggers);
    const meta = primary ? triggerMeta(primary) : null;
    refs.mark.dataset.tone = meta ? meta.tone : "neutral";
    if (refs.markIcon.tagName.toLowerCase() === "ha-icon") {
      refs.markIcon.setAttribute("icon", meta ? meta.icon : "mdi:record-circle-outline");
    }

    refs.time.textContent = formatTime(event.start);
    refs.camera.textContent = event.camera;

    // Trigger chips
    const triggers = sortTriggers(event.triggers || []);
    refs.chips.replaceChildren();
    if (triggers.length === 0) {
      refs.chips.append(
        el("span", { class: "chip", dataset: { tone: "neutral" } }, icon("mdi:video-outline"), "Recording")
      );
    } else {
      // How many times each trigger actually fired, where the recorder can say. The NVR
      // only reports that a segment carried a person, never that it carried three.
      const counts = store.detectionCounts(event);
      for (const trigger of triggers) {
        const info = triggerMeta(trigger);
        const count = counts.get(trigger) || 0;
        refs.chips.append(
          el(
            "span",
            { class: "chip", dataset: { tone: info.tone } },
            icon(info.icon),
            count > 1 ? `${info.label} (${count})` : info.label
          )
        );
      }
    }

    // Facts: duration, size and which resolutions the clip exists in.
    refs.facts.replaceChildren();
    refs.facts.append(
      el("div", {
        class: "facts__main tabular",
        // The device reports one size for a whole recording, so it is only shown when
        // this row *is* the whole recording. Otherwise a slice of 24/7 footage would
        // claim the parent file's hundreds of megabytes.
        text: event.size_is_exact
          ? `${formatDuration(event.duration)} · ${formatSize(event.size)}`
          : formatDuration(event.duration),
      })
    );

    if (!event.playable) {
      refs.facts.append(el("span", { class: "badge badge--error" }, "Unplayable"));
    } else {
      // Only the resolution actually searched is asserted here. Discovering the others
      // would cost a second search per camera-day, so the player resolves them on demand.
      refs.facts.append(
        el(
          "div",
          { class: "streams" },
          ...event.streams.map((stream) =>
            el(
              "span",
              { class: "badge", title: `Found in ${streamLabel(stream)} resolution` },
              streamLabel(stream)
            )
          )
        )
      );
    }

    node.dataset.selected = String(store.selectedEventId === event.id);
    node.dataset.playable = String(Boolean(event.playable));
    node.setAttribute("aria-selected", String(store.selectedEventId === event.id));
    node.disabled = !event.playable;

    node.onclick = () => {
      this.dispatchEvent(new CustomEvent("event-selected", { detail: event, bubbles: true, composed: true }));
    };
  }

  // ------------------------------------------------------------ keyboard nav

  _onKeydown(keyEvent) {
    if (keyEvent.key !== "ArrowDown" && keyEvent.key !== "ArrowUp") return;
    const rows = [...this._list.querySelectorAll("button.event:not([disabled])")];
    if (rows.length === 0) return;
    const index = rows.indexOf(keyEvent.composedPath()[0]);
    const next = rows[index + (keyEvent.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    keyEvent.preventDefault();
    next.focus();
    next.scrollIntoView({ block: "nearest" });
  }

  /** Move focus to the row for an event, used when stepping from the player. */
  focusEvent(id) {
    const rows = [...this._list.querySelectorAll("button.event")];
    const row = rows.find((node) => node.__key === id);
    if (row) row.scrollIntoView({ block: "nearest" });
  }
}

// Guarded: Home Assistant may import this module more than once (after an
// update, or from a cached copy), and a duplicate define() throws and takes the
// whole panel down with it.
if (!customElements.get("reolink-event-list")) customElements.define("reolink-event-list", EventList);
