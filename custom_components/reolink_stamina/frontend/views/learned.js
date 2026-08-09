/**
 * What one camera has learned, as distributions rather than as a verdict.
 *
 * The event sheet answers "why was this one marked". This answers the question underneath it
 * — what was it compared against — and it is the only way to see that a camera has learned
 * something wrong. A fortnight of scaffolding outside, a sensor that flapped for a week: both
 * are invisible in a list of events and obvious in a distribution.
 *
 * Every chart here plots one series. That is a deliberate constraint rather than a limitation
 * of the data: with one series there is no identity to encode, so no palette to get wrong, no
 * legend to read, and colour-blind safety is not a question that arises. Height carries the
 * magnitude; the single accent hue carries nothing at all, which is the point. Colouring bars
 * by their own value is the commonest way to make a chart look designed and say less.
 */

import { adoptStyles, el, icon, reconcile } from "../dom.js";
import { SHARED } from "../theme.js";

const STYLES = /* css */ `
:host { display: contents; }

.sheet {
  width: min(720px, calc(100vw - 28px));
  max-height: min(86vh, 820px);
  padding: 0;
  border: 1px solid var(--rv-line);
  border-radius: var(--rv-radius);
  background: var(--rv-surface);
  color: var(--rv-text);
  box-shadow: var(--rv-shadow-lifted);
  overflow: hidden;
}
/* Scoped to [open]: a dialog's own "display: none" while closed comes from the browser's
   stylesheet, so an unconditional display here means it never closes again. */
.sheet[open] { display: flex; flex-direction: column; }
.sheet::backdrop { background: rgba(0, 0, 0, 0.42); }
/* The dialog takes focus itself so the close button does not open with a ring around it.
   Safari then draws the ring around the whole modal, which is never useful: a dialog is not
   a control, and everything inside it that is keeps its own. */
.sheet:focus, .sheet:focus-visible { outline: none; }

/*
 * Full-screen on a phone. A dialog inset by fourteen pixels on a 390px screen is a box with a
 * sliver of dimmed panel around it — the inset reads as an accident rather than as a frame,
 * and it costs the width the content actually wanted.
 */
@media (max-width: 700px) {
  .sheet {
    /* Fixed rather than covering the screen by size alone: a modal dialog is centred by the
       browser, and one exactly as tall as the viewport still moves when the keyboard opens. */
    position: fixed;
    inset: 0;
    width: 100vw;
    max-width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
    margin: 0;
    border: 0;
    border-radius: 0;
    /* Being placed against the viewport is also how it escapes the offset Home Assistant's
       layout starts at, so at this size nothing is holding it off the status bar but this —
       the same case the full-screen player already handles. Without it the camera's name and
       the close button sit under the clock. */
    padding: var(--rv-safe-top) var(--rv-safe-right) var(--rv-safe-bottom) var(--rv-safe-left);
  }
}


.head {
  flex: 0 0 auto;
  display: flex; align-items: flex-start; gap: 12px;
  padding: 20px 24px 14px;
  border-bottom: 1px solid var(--rv-line);
}
.head__body { flex: 1; min-width: 0; }
.head__eyebrow {
  font-size: 0.74rem; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--rv-text-dim);
}
.head__title { font-size: 1.16rem; font-weight: 650; letter-spacing: -0.01em; margin-top: 3px; }
.head__sub { font-size: 0.82rem; color: var(--rv-text-dim); margin-top: 5px; }
.close {
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; padding: 4px; border-radius: 50%;
  border: 1px solid transparent; background: none; cursor: pointer;
  color: var(--rv-text-dim);
}
.close:hover { background: color-mix(in srgb, var(--rv-text) 10%, transparent); }
.close .icon { display: block; }

.scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; }
.body { padding: 18px 24px 24px; }

/* Filters in one row above the charts, which is where a filter belongs: change what is
   plotted without moving your eye away from the plot. */
.kinds { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 20px; }
.kind {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 12px 5px 9px; border-radius: 999px;
  border: 1px solid var(--rv-line); background: none; color: var(--rv-text-dim);
  font-size: 0.8rem; font-weight: 500; cursor: pointer;
  transition: border-color 120ms var(--rv-ease), color 120ms var(--rv-ease);
}
.kind:hover { color: var(--rv-text); }
.kind[aria-pressed="true"] {
  border-color: color-mix(in srgb, var(--rv-accent) 55%, transparent);
  background: color-mix(in srgb, var(--rv-accent) 12%, transparent);
  color: var(--rv-text);
}
.kind__count { font-variant-numeric: tabular-nums; opacity: 0.72; }
.kind .icon { --mdc-icon-size: 15px; width: 15px; height: 15px; display: block; }

.section { margin-top: 26px; }
.section:first-of-type { margin-top: 0; }
.section__title { font-size: 0.94rem; font-weight: 600; }
.section__note { font-size: 0.8rem; color: var(--rv-text-dim); margin-top: 3px; }

/*
 * The hourly columns. Twenty-four slots, each holding a fixed-width mark with the leftover
 * width left as air — a bar that fills its slot touches its neighbour and the two read as one
 * block. The hit target is the whole slot, full height, so a one-pixel bar at 04:00 is still
 * comfortably hoverable.
 */
.clock { margin-top: 14px; }
.clock__plot {
  position: relative;
  display: flex; align-items: flex-end; gap: 2px;
  height: 128px;
  border-bottom: 1px solid var(--rv-line);
}
.slot {
  flex: 1 1 0; min-width: 0; height: 100%;
  display: flex; align-items: flex-end; justify-content: center;
  border: 0; background: none; padding: 0; cursor: default;
}
.slot__mark {
  width: min(16px, 100%);
  min-height: 2px;
  border-radius: 4px 4px 0 0;
  background: color-mix(in srgb, var(--rv-accent) 68%, transparent);
  transition: background 100ms var(--rv-ease);
}
.slot:hover .slot__mark, .slot:focus-visible .slot__mark { background: var(--rv-accent); }
.slot__mark[data-empty="true"] { background: var(--rv-line); }

.clock__axis {
  display: flex; margin-top: 7px;
  font-size: 0.7rem; color: var(--rv-text-dim); font-variant-numeric: tabular-nums;
}
.tick { flex: 1 1 0; text-align: center; }
.tick[data-quiet="true"] { visibility: hidden; }

.readout {
  margin-top: 10px; min-height: 1.15em;
  font-size: 0.8rem; color: var(--rv-text-dim); font-variant-numeric: tabular-nums;
}
.readout b { color: var(--rv-text); font-weight: 600; }

/*
 * The categorical distributions. A bar and its numbers on one line: the label names it, the
 * share is the mark, and the count sits at the tip where a column would carry its cap value.
 */
.rows { margin-top: 12px; display: flex; flex-direction: column; gap: 9px; }
.row { display: grid; grid-template-columns: minmax(0, 8.5rem) 1fr auto; align-items: center; gap: 12px; }
.row__label { font-size: 0.83rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row__track { height: 9px; border-radius: 999px; background: color-mix(in srgb, var(--rv-text) 7%, transparent); }
.row__fill {
  height: 100%; border-radius: 999px; min-width: 3px;
  background: color-mix(in srgb, var(--rv-accent) 68%, transparent);
}
.row__value {
  font-size: 0.78rem; color: var(--rv-text-dim);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}

/*
 * The long tail, folded away.
 *
 * A busy camera has fired after eleven other cameras, and eight of those account for two
 * percent between them. Listing all eleven buries the two that matter under nine that do not;
 * hiding them outright would be lying about what was counted. A details element says both —
 * and it is the browser's own, so it opens with a keyboard and prints expanded.
 */
.more { margin-top: 9px; }
.more > summary {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 2px; border-radius: var(--rv-radius-sm);
  font-size: 0.78rem; color: var(--rv-text-dim); cursor: pointer;
  list-style: none;
}
.more > summary::-webkit-details-marker { display: none; }
.more > summary:hover { color: var(--rv-text); }
.more > summary .icon {
  --mdc-icon-size: 15px; width: 15px; height: 15px; display: block;
  transition: transform 140ms var(--rv-ease);
}
.more[open] > summary .icon { transform: rotate(180deg); }
.more[open] > summary .more__show { display: none; }
.more:not([open]) > summary .more__hide { display: none; }
.more .rows { margin-top: 9px; }

.note {
  margin-top: 22px; padding: 12px 14px;
  border: 1px solid var(--rv-line); border-radius: var(--rv-radius-sm);
  font-size: 0.82rem; color: var(--rv-text-dim);
}
.state { display: flex; align-items: flex-start; gap: 10px; }
.state .icon { flex: 0 0 auto; color: var(--rv-text-dim); }
`;

const KIND_ICONS = {
  person: "mdi:walk",
  vehicle: "mdi:car",
  animal: "mdi:paw",
  package: "mdi:package-variant-closed",
  face: "mdi:face-recognition",
  doorbell: "mdi:doorbell",
  crying: "mdi:emoticon-cry-outline",
  motion: "mdi:motion-sensor",
};

const KIND_LABELS = {
  person: "Person",
  vehicle: "Vehicle",
  animal: "Animal",
  package: "Package",
  face: "Face",
  doorbell: "Doorbell",
  crying: "Crying",
  motion: "Motion",
};

/*
 * What stays visible, and what folds away.
 *
 * A fixed count is the wrong instrument. "What fired before it" is usually one dominant
 * answer and a long tail of near-zeroes, where one line is the whole finding — but the same
 * section on another camera reads 58% / 32%, and folding the 32% hides half the story to
 * save one line. The shape of the distribution has to decide, not its length.
 *
 * So a row is shown while either is true: it is big enough to matter on its own, or the rows
 * above it have not yet accounted for most of what happened. A dominant-plus-tail distribution
 * collapses to one line; an evenly spread one shows enough rows to cover the majority.
 */
// A row worth this much is never folded, wherever it sits in the list.
const ALWAYS_SHOW_ABOVE = 0.25;
// And rows keep being shown until between them they account for this much of the total.
const COVER_AT_LEAST = 0.5;
// Except that a genuinely flat distribution — twenty values at five percent — would show
// eleven rows to reach half, which is a list rather than a finding.
const MOST_ROWS = 8;
// And there is always a first row, however completely it dominates.
const LEAST_ROWS = 1;
// A distribution this short is shown whole. Folding two of four rows saves two lines and
// costs a click, and the fold itself takes one of the lines back.
const NEVER_FOLD_UNDER = 5;

// This used to be a floor per section — four for the durations — and the floor won every
// argument with the rule above it: 53% / 30% / 12% / 3% showed all four rows because four was
// the minimum, when the finding was plainly the first two.

function plural(count, one, many) {
  return count === 1 ? one : many;
}

export class LearnedSheet extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
    this._kind = null;
  }

  set api(api) {
    this._api = api;
  }

  /**
   * Open for one camera, or for several at once.
   *
   * The same sheet either way. Across several, "which camera" becomes a distribution like any
   * other — and on a property where one camera fires ten times more than the rest, that row is
   * usually the most useful thing on the screen.
   */
  async open({ title, eyebrow, targets }) {
    if (!this._built) this._build();
    this._kind = null;
    this._profile = null;

    this._title.textContent = title;
    this._eyebrow.textContent = eyebrow;
    this._sub.textContent = "Reading what has been learned…";
    this._body.replaceChildren();
    this._sheet.showModal();

    try {
      this._profile = await this._api.relevanceProfile(targets);
    } catch (err) {
      this._sub.textContent = "";
      this._body.replaceChildren(
        el("div", { class: "note", text: String(err?.message || err) })
      );
      return;
    }
    this._render();
  }

  _build() {
    this._eyebrow = el("div", { class: "head__eyebrow" });
    this._title = el("div", { class: "head__title" });
    this._sub = el("div", { class: "head__sub" });
    this._body = el("div", { class: "body" });

    this._sheet = el(
      "dialog",
      {
        class: "sheet",
        // showModal focuses the first focusable child, which would open the sheet with a
        // ring around its close button. The dialog takes focus itself instead.
        tabindex: "-1",
        autofocus: true,
      },
      el(
        "div",
        { class: "head" },
        el("div", { class: "head__body" }, this._eyebrow, this._title, this._sub),
        el(
          "button",
          {
            class: "close",
            "aria-label": "Close",
            onclick: () => this._sheet.close(),
          },
          icon("mdi:close")
        )
      ),
      el("div", { class: "scroll" }, this._body)
    );

    this.shadowRoot.append(this._sheet);
    this._built = true;
  }

  _render() {
    const profile = this._profile;
    if (!profile?.enabled) {
      this._sub.textContent = "";
      this._body.replaceChildren(
        el("div", {
          class: "note",
          text: "Learn what is normal is switched off, so nothing has been counted yet.",
        })
      );
      return;
    }

    const { days = 0, events = 0 } = profile.coverage || {};
    this._sub.textContent = `${events.toLocaleString()} ${plural(events, "detection", "detections")} over ${days} ${plural(days, "day", "days")}`;

    if (!profile.kinds?.length) {
      this._body.replaceChildren(
        el(
          "div",
          { class: "note state" },
          icon("mdi:progress-clock"),
          el("span", {
            text: "Nothing counted for this camera yet. It starts the moment a detection fires, and whatever Home Assistant already had in its history was imported when the panel was set up.",
          })
        )
      );
      return;
    }

    if (!this._kind || !profile.kinds.some((entry) => entry.kind === this._kind)) {
      this._kind = profile.kinds[0].kind;
    }

    this._body.replaceChildren(
      this._renderKinds(profile),
      ...this._renderSelected(profile),
      ...(profile.state === "active"
        ? []
        : [
            el(
              "div",
              { class: "note state" },
              icon("mdi:progress-clock"),
              el("span", {
                text:
                  profile.state === "too_few_events"
                    ? "Counted, but not yet enough to compare against — so nothing on this camera is being marked unusual."
                    : "Still collecting. What is below is real; there is not yet enough of it to call anything unusual.",
              })
            ),
          ])
    );
  }

  _renderKinds(profile) {
    const wrap = el("div", { class: "kinds" });
    for (const entry of profile.kinds) {
      wrap.append(
        el(
          "button",
          {
            class: "kind",
            "aria-pressed": entry.kind === this._kind ? "true" : "false",
            onclick: (event) => {
              event.currentTarget.blur();
              this._kind = entry.kind;
              this._render();
            },
          },
          icon(KIND_ICONS[entry.kind] || "mdi:motion-sensor"),
          el("span", { text: KIND_LABELS[entry.kind] || entry.kind }),
          el("span", { class: "kind__count", text: entry.events.toLocaleString() })
        )
      );
    }
    return wrap;
  }

  _renderSelected(profile) {
    const chosen = profile.kinds.find((entry) => entry.kind === this._kind);
    if (!chosen) return [];

    const name = (KIND_LABELS[chosen.kind] || chosen.kind).toLowerCase();
    const sections = [this._renderClock(chosen, name)];

    if (chosen.cameras?.length) {
      sections.push(
        this._renderRows(
          "Which camera",
          "Across everything selected, so a camera on a busy road can be most of what is here.",
          chosen.cameras
        )
      );
    }
    sections.push(
      this._renderRows("How long it lasts", null, this._orderDurations(chosen.duration))
    );

    if (chosen.predecessor?.length) {
      sections.push(
        this._renderRows(
          "What fired before it",
          "The camera whose detection came immediately before this one, anywhere on the property.",
          chosen.predecessor
        )
      );
    }
    for (const signal of chosen.signals || []) {
      sections.push(
        this._renderRows(signal.label, "What this was doing when the camera fired.", signal.values)
      );
    }
    return sections;
  }

  /**
   * Duration reads shortest to longest, never by how common each is.
   *
   * The buckets are powers of two and arrive labelled that way, so the order is the number
   * inside the label. Sorting these by share would put "~32s" between "~4s" and "~8s" and
   * make a distribution look like noise.
   */
  _orderDurations(values) {
    const seconds = (entry) => Number.parseFloat(String(entry.value).replace(/[^\d.]/g, "")) || 0;
    return [...values].sort((left, right) => seconds(left) - seconds(right));
  }

  _renderClock(chosen, name) {
    const hours = chosen.clock || [];
    const busiest = Math.max(1, ...hours.map((hour) => hour.events));
    const total = hours.reduce((sum, hour) => sum + hour.events, 0);

    const readout = el("div", { class: "readout" });
    const plot = el("div", { class: "clock__plot" });

    for (const hour of hours) {
      const mark = el("div", {
        class: "slot__mark",
        "data-empty": hour.events === 0 ? "true" : "false",
        style: { height: `${Math.max(2, Math.round((hour.events / busiest) * 100))}%` },
      });
      const share = total ? Math.round((hour.events / total) * 100) : 0;
      const slot = el(
        "button",
        {
          class: "slot",
          "aria-label": `${String(hour.hour).padStart(2, "0")}:00 — ${hour.events} ${plural(hour.events, "detection", "detections")}`,
          onmouseenter: () => {
            readout.replaceChildren(
              el("b", { text: `${String(hour.hour).padStart(2, "0")}:00` }),
              document.createTextNode(
                ` — ${hour.events.toLocaleString()} ${plural(hour.events, "detection", "detections")}${share ? `, ${share}% of the day` : ""}`
              )
            );
          },
          onfocus: (event) => event.currentTarget.dispatchEvent(new Event("mouseenter")),
        },
        mark
      );
      plot.append(slot);
    }

    // Every fourth hour is labelled. Twenty-four numbers under a 700px chart collide, and a
    // reader counting bars from 00:00 is doing arithmetic the axis was supposed to save them.
    const axis = el("div", { class: "clock__axis" });
    for (const hour of hours) {
      axis.append(
        el("div", {
          class: "tick",
          "data-quiet": hour.hour % 4 === 0 ? "false" : "true",
          text: String(hour.hour).padStart(2, "0"),
        })
      );
    }

    plot.addEventListener("mouseleave", () => readout.replaceChildren());

    return el(
      "div",
      { class: "section" },
      el("div", { class: "section__title", text: `When it sees a ${name}` }),
      el("div", {
        class: "section__note",
        text: "By hour of the local day, over everything collected. Recent weeks count for more than old ones.",
      }),
      el("div", { class: "clock" }, plot, axis, readout)
    );
  }

  _renderRows(title, note, values) {
    // Everything is measured against the commonest value across the whole distribution, not
    // against whichever slice it landed in — otherwise the folded rows would redraw at their
    // own scale and a 1% value would look as big as a 60% one.
    const widest = Math.max(0.0001, ...values.map((entry) => entry.share));

    // Chosen by share, kept in display order. Slicing the array would fold whatever happened
    // to be at the end, which for the durations is the longest rather than the rarest.
    const ranked = [...values].sort((left, right) => right.share - left.share);
    const keep = new Set();
    let covered = 0;
    for (const entry of values.length < NEVER_FOLD_UNDER ? values : ranked) {
      if (values.length < NEVER_FOLD_UNDER) {
        keep.add(entry.value);
        continue;
      }
      const worthIt =
        keep.size < LEAST_ROWS || entry.share >= ALWAYS_SHOW_ABOVE || covered < COVER_AT_LEAST;
      if (!worthIt || keep.size >= MOST_ROWS) break;
      keep.add(entry.value);
      covered += entry.share;
    }
    const shown = values.filter((entry) => keep.has(entry.value));
    const folded = values.filter((entry) => !keep.has(entry.value));

    const section = el(
      "div",
      { class: "section" },
      el("div", { class: "section__title", text: title }),
      ...(note ? [el("div", { class: "section__note", text: note })] : []),
      this._rows(shown, widest)
    );

    if (folded.length) {
      const share = folded.reduce((sum, entry) => sum + entry.share, 0);
      section.append(
        el(
          "details",
          { class: "more" },
          el(
            "summary",
            {},
            icon("mdi:chevron-down"),
            el("span", {
              class: "more__show",
              text: `${folded.length} less common ${plural(folded.length, "one", "ones")}, ${Math.max(1, Math.round(share * 100))}% between them`,
            }),
            el("span", { class: "more__hide", text: "Hide the less common ones" })
          ),
          this._rows(folded, widest)
        )
      );
    }
    return section;
  }

  _rows(values, widest) {
    const rows = el("div", { class: "rows" });
    reconcile(
      rows,
      values,
      (entry) => entry.value,
      () => {
        const refs = {};
        refs.label = el("div", { class: "row__label" });
        refs.fill = el("div", { class: "row__fill" });
        refs.value = el("div", { class: "row__value" });
        const row = el(
          "div",
          { class: "row" },
          refs.label,
          el("div", { class: "row__track" }, refs.fill),
          refs.value
        );
        row.__refs = refs;
        return row;
      },
      (row, entry) => {
        const refs = row.__refs;
        refs.label.textContent = entry.label;
        refs.label.title = entry.label;
        refs.fill.style.width = `${Math.max(2, Math.round((entry.share / widest) * 100))}%`;
        // A value below half a percent is not "0%" — it is rare, which is the entire point
        // of the feature. Below the rounding it says so with the inequality rather than a
        // word, which is shorter and reads as a number beside the numbers around it.
        const percent = entry.share * 100;
        const share = percent >= 1 ? `${Math.round(percent)}%` : percent > 0 ? "<1%" : "0%";
        refs.value.textContent = `${share} · ${entry.events.toLocaleString()}`;
      }
    );
    return rows;
  }
}

if (!customElements.get("reolink-learned-sheet")) {
  customElements.define("reolink-learned-sheet", LearnedSheet);
}
