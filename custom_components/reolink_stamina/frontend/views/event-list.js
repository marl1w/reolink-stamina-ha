/**
 * The event timeline.
 *
 * Rows are reconciled by event id rather than rebuilt, so the background patches that
 * arrive as a device answers never move the scroll position, drop keyboard focus, or
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
  triggerMeta,
} from "../format.js";

const STYLES = /* css */ `
:host { display: block; height: 100%; }

.list { padding: 8px 12px 40px; }

/*
 * No backdrop-filter here, deliberately.
 *
 * A blurred backdrop on a sticky element inside a scroller is about the most expensive
 * thing that can be put in one: the backdrop behind it changes every single frame as rows
 * pass underneath, so the blur is re-sampled and re-run every frame, on the GPU, for as
 * long as the finger is moving. On a phone that is most of the frame budget.
 *
 * It was also buying almost nothing. The gradient below is opaque for most of its height,
 * so the blur only ever applied to the fade at the bottom edge — a few pixels of softness
 * for a per-frame cost across the whole scroll. The fade now runs further before it gives
 * out, which covers the same rows for free.
 */
.day {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 8px 8px;
  background: linear-gradient(to bottom, var(--rv-bg) 82%, transparent);
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
/*
 * Time and camera on one line while they fit, and the camera on its own line when they do
 * not — which is the list at its narrowest, with the player dragged most of the way over.
 *
 * Wrapping rather than an ellipsis, deliberately. The name is what says which camera this
 * row is, and truncating is the one thing that can make it stop saying it: "Driveway" and
 * "Driveway (side)" both read as "Drivew…". A row that is one line taller costs a fraction
 * of what a row you have to click to identify does. The row gap is small because these two
 * are one headline in two lines, not two separate things.
 */
.headline { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; min-width: 0; }
.time { font-size: 1rem; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
/* Long names are not sentences — "Front_Door_Left" has nowhere it would rather break, so it
   is allowed to break anywhere rather than run out of the row. */
.camera { font-size: 0.84rem; color: var(--rv-text-dim); min-width: 0; overflow-wrap: anywhere; }
.chips { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
/*
 * The icon of a chip whose kind was marked turns red, while the chip keeps its own colour.
 *
 * The mark at the head of the row says that something in this recording stood out; on a row
 * holding a person and a vehicle it does not say which. Colouring the whole chip would cost
 * the tone that says what was detected, so only the glyph changes.
 */
.chip[data-unusual="true"] .icon { color: var(--rv-tone-alert); }

/*
 * On a phone the chips lose their words and keep their icons.
 *
 * Four chips of text is most of the row's width, and it is the least of what the row says:
 * the time, the camera and the mark are what somebody scans for. The icon and the tone carry
 * the same identity in a fifth of the space, and the count stays because no icon can say it.
 * The label is still in the accessible name and still in the sheet.
 */
@media (max-width: 700px) {
  .chips .chip { padding: 4px 7px; gap: 4px; }
  .chips .chip__label { display: none; }
}

/* ------------------------------------------------- learning what is normal
 *
 * Only the outliers are marked, and nothing says "common".
 *
 * Two reasons, and the second is the one that matters. A mark on nineteen rows in twenty is
 * noise people stop seeing, which devalues the twentieth. And rarity is not risk: a burglar
 * at three in the afternoon in a busy driveway is statistically unremarkable, so a row
 * labelled "common" would be the panel making a safety claim it has no basis for.
 *
 * Not a warning triangle and not red, for the same reason — this is "you probably want to
 * look at this", not an alarm.
 */
/*
 * The mark sits with the detection chips, not with the measurements.
 *
 * It says what the row *is*, which is what that row of chips is for — and putting it there
 * rather than in the right-hand group means it costs no fixed column, so nothing else in the
 * row moves according to whether it is present.
 *
 * Icon only, at every width. The word added eighty pixels to say what the colour and the
 * shape already say, and the tooltip and the label for assistive technology carry it for
 * anyone the shape does not reach.
 */
/*
 * The mark is one glyph, in the same box as the ⓘ it stands in place of.
 *
 * It used to be a red pill with a round glyph inside it, which is a dot within a dot — two
 * circles of nearly the same size, and no amount of padding makes one look centred inside the
 * other when both are drawn from different metrics. Sharing the ⓘ's geometry exactly means
 * the two are interchangeable by construction: a row with a mark and a row without cannot
 * shift by a pixel, because the box is identical and only the glyph and its colour differ.
 */

/*
 * The way in to the numbers, on every row and always visible.
 *
 * It used to fade in on hover, with a "hover: none" rule to bring it back on touch. Two
 * problems with that: a touch laptop reports "hover: hover" and got neither, and a control
 * you can only find by hovering is one most people never find at all. Dim rather than
 * hidden — quiet enough not to compete with the row, present enough to be discovered.
 */
/* Exactly the box the unusual mark occupies, down to the border, because the two stand in
   the same
   place and one being two pixels smaller reads as a mistake rather than as a difference.
   A transparent border rather than none: a zero border makes the button 2px narrower than
   the pill beside it, which is the whole of the discrepancy. */
.odd,
.why {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  background: none;
  /* The glyph grew and the padding shrank to match, so the box is the 24px it always was.
     Growing the box instead made the chip row taller, which pushed everything in the row's
     right-hand group out of line with it — the play button most visibly. */
  padding: 2px;
  border-radius: 50%;
  color: color-mix(in srgb, var(--rv-text-dim) 75%, transparent);
  cursor: pointer;
  transition: color 120ms var(--rv-ease);
  flex: 0 0 auto;
}
.why:hover, .why:focus-visible { color: var(--rv-text); }
/* After the shared rule, not before it: the two share a selector for their box and this is
   the one line where they differ, so it has to be the one that wins. */
.odd { color: var(--rv-tone-alert); }
.odd:hover { color: color-mix(in srgb, var(--rv-tone-alert) 75%, var(--rv-text)); }
/* 18px rather than the 14 the pill used to hold: without a pill around it the glyph *is* the
   control, so it has to carry the weight the border used to. The padding above gives back
   exactly what this takes, so the row's metrics do not move. */
.odd .icon,
.why .icon { --mdc-icon-size: 18px; width: 18px; height: 18px; display: block; }

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

/* The headline stacks itself now, at whatever width it runs out of room — so what is left
   here is the phone's own two: tighter padding, and the row's length and size, which is the
   widest thing in a row and the least of what it says. */
@media (max-width: 620px) {
  .list { padding: 6px 10px 32px; }
  .facts__main { display: none; }
  /* On a phone eighty reserved pixels is most of what the row has. The mark keeps its
     colour and its shape, which is what carries the meaning; the word is what goes. */
  .side { grid-template-columns: auto auto; }
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

  /**
   * Ask for the numbers behind a row.
   *
   * Raised rather than handled, because the sheet is a sibling element the panel owns: a
   * dialog parented inside this scroller would be clipped by it, and a modal one belongs
   * beside the list rather than in it.
   */
  _ask(event) {
    this.dispatchEvent(
      new CustomEvent("show-relevance", { bubbles: true, composed: true, detail: { event } })
    );
  }

  // ------------------------------------------------- learning what is normal

  /**
   * Show what the model collected about one row.
   *
   * Opened for any row, not only a marked one. Seeing why an ordinary event is ordinary is
   * what makes the mark believable when it does appear — and while a camera is still
   * collecting it is the only thing there is to show, which is precisely when somebody
   * wants to know whether this is working at all.
   */
  _pressable({ onOpen, ...props }, ...children) {
    const open = (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      // A pointer click leaves the ring behind after the dialog closes, because closing
      // returns focus to whatever opened it and a span with a tabindex gets `:focus-visible`
      // from a click where a real button would not. Dropping focus first means the ring
      // never comes back — while a keyboard activation, which reports a detail of zero,
      // keeps it, because there it is the only way of knowing where you are.
      if (ev.type === "click" && ev.detail > 0) ev.currentTarget.blur();
      onOpen();
    };
    return el(
      "span",
      {
        ...props,
        role: "button",
        tabindex: "0",
        onclick: open,
        onkeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") open(ev);
        },
      },
      ...children
    );
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
            text: `Could not reach the device${age ? `, showing results from ${age}` : ""}. ${store.errors[0]}`,
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
                ? "Still searching the device…"
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
    refs.camera = el("span", { class: "camera" });
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
      el(
        "div",
        { class: "side" },
        refs.facts,
        icon("mdi:play-circle-outline", "go")
      )
    );
    node.__refs = refs;
    return node;
  }

  _updateEvent(node, event) {
    const refs = node.__refs;
    const store = this._store;

    // Leading mark reflects the most significant trigger.
    const kinds = store.eventKinds(event);
    const primary = primaryTrigger(kinds);
    const meta = primary ? triggerMeta(primary) : null;
    refs.mark.dataset.tone = meta ? meta.tone : "neutral";
    if (refs.markIcon.tagName.toLowerCase() === "ha-icon") {
      refs.markIcon.setAttribute("icon", meta ? meta.icon : "mdi:record-circle-outline");
    }

    refs.time.textContent = formatTime(event.start);
    refs.camera.textContent = event.camera;

    // Trigger chips. `eventKinds` puts Home Assistant's detection sensors ahead of the
    // recorder's own tags, so a camera the device never labels still reads as what was seen.
    refs.chips.replaceChildren();

    // One slot at the head of the chips, holding whichever applies: the mark on a row that
    // stood out, the way in to the numbers on every other. Same place on every row, so the
    // eye finds it without hunting, and both open the same thing.
    if (store.relevanceFor(event.entry_id, event.channel)) {
      refs.chips.append(
        store.isUnusual(event)
          ? this._pressable(
              {
                class: "odd",
                title: "Unusual for this camera — tap to see why",
                "aria-label": "Unusual for this camera",
                onOpen: () => this._ask(event),
              },
              icon("mdi:alert-circle")
            )
          : this._pressable(
              {
                class: "why",
                title: "What has been learned about this",
                "aria-label": "What has been learned about this event",
                onOpen: () => this._ask(event),
              },
              icon("mdi:information-outline")
            )
      );
    }

    if (kinds.length === 0) {
      refs.chips.append(
        el("span", { class: "chip", dataset: { tone: "neutral" } }, icon("mdi:video-outline"), "Recording")
      );
    } else {
      // How many times each trigger actually fired, where the recorder can say. A device
      // only reports that a segment carried a person, never that it carried three.
      const counts = store.detectionCounts(event);
      // Which *kinds* were marked, so a row holding a person and a vehicle can say which of
      // them stood out. The mark at the head says only that something here did, and on a row
      // with three detections in it that leaves the reader to guess.
      const odd = new Set(
        store
          .eventRelevance(event)
          .filter((item) => item.unusual)
          .map((item) => item.kind)
      );
      for (const trigger of kinds) {
        const info = triggerMeta(trigger);
        const count = counts.get(trigger) || 0;
        refs.chips.append(
          el(
            "span",
            {
              class: "chip",
              dataset: { tone: info.tone, unusual: String(odd.has(trigger)) },
            },
            icon(info.icon),
            el("span", { class: "chip__label", text: info.label }),
            // Outside the label, so it survives the phone: the icon says what was detected
            // and the count says how many, and only the first of those is redundant.
            ...(count > 1 ? [el("span", { class: "chip__count", text: `(${count})` })] : [])
          )
        );
      }
    }

    // Facts: duration and size. Deliberately not the resolutions — see below.
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

    // No resolution badges. Only the stream actually searched is known here, so every row
    // claimed "Low" and nothing else — while the same clip downloads perfectly well in
    // high, because the player resolves the other resolution when asked. Being truthful
    // would cost a second search per camera-day against a recorder that is slow enough
    // already, and the choice belongs in the player, where it is offered and where it is
    // already known by the time it matters.
    if (!event.playable) {
      refs.facts.append(el("span", { class: "badge badge--error" }, "Unplayable"));
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
