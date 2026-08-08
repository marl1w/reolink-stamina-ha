/**
 * The numbers behind one row, and the paging between the detections inside it.
 *
 * Its own element rather than a section of the list, because the two answer different
 * questions and shared only a store: the list is a scroller of hundreds of rows reconciled
 * on every patch, and this is one dialog rendered when somebody asks for it. Keeping them
 * together meant every change to a bar chart risked the scroll position of the list.
 *
 * A sheet rather than a popover: positioned against a row inside a scroller, a popover gets
 * clipped by the scroller sooner or later, and on a phone a sheet is what people expect.
 */

import { adoptStyles, el, icon } from "../dom.js";
import { SHARED } from "../theme.js";
import { formatDayLabel, formatTime, triggerMeta } from "../format.js";

const STYLES = /* css */ `
:host { display: contents; }

/* ---------------------------------------------- the numbers behind a row
 *
 * A sheet rather than a popover: positioned against a row inside a scroller, a popover gets
 * clipped by the scroller sooner or later, and on a phone a sheet is what people expect.
 *
 * The contributions are signed around zero — positive is rarer than chance, negative is more
 * common — so they are drawn as a diverging bar either side of a centre line rather than
 * listed as numbers. Which signal made an event stand out is then the longest bar, readable
 * without arithmetic, and "everything sits near the middle" is what an ordinary event looks
 * like. The numbers stay beside them, because the bar is the summary and the figure is the
 * fact.
 */
.sheet {
  width: min(680px, calc(100vw - 28px));
  max-height: min(86vh, 820px);
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
    width: 100vw;
    max-width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
    margin: 0;
    border: 0;
    border-radius: 0;
    /* Fixed rather than covering the screen by size alone: a modal dialog is centred by the
       browser, and one exactly as tall as the viewport still moves when the keyboard opens. */
    padding-bottom: var(--rv-safe-bottom, 0px);
  }
}

.sheet__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}
/*
 * A segment holds whatever fired inside it, so a busy evening is one row with three
 * detections in it. Stacked, they were a long scroll with no sense of how many there were
 * or which one you were on; paged, the count is stated and each one gets the whole sheet.
 */
.sheet__foot {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 24px 16px;
  border-top: 1px solid var(--rv-line);
}
.sheet__foot[hidden] { display: none; }
.sheet__count { font-size: 0.78rem; color: var(--rv-text-dim); font-variant-numeric: tabular-nums; }
.sheet__pager { display: flex; gap: 8px; }

.sheet__head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  padding: 20px 24px 0;
}
.sheet__when { font-size: 0.74rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--rv-text-dim); }
.sheet__title { font-size: 1.16rem; font-weight: 650; letter-spacing: -0.01em; margin-top: 3px; }
.sheet__close {
  border: 0; background: none; color: var(--rv-text-dim); cursor: pointer;
  padding: 4px; border-radius: 50%; display: inline-flex; flex: 0 0 auto;
}
.sheet__close:hover { background: color-mix(in srgb, var(--rv-text) 10%, transparent); }

/* The sheet with nothing to show, made to look like a state rather than a failure. */
.blank { padding: 30px 24px 34px; text-align: center; }
.blank .icon {
  --mdc-icon-size: 30px; width: 30px; height: 30px;
  color: color-mix(in srgb, var(--rv-text-dim) 60%, transparent);
}
.blank__body { margin-top: 12px; font-size: 0.9rem; }
.blank__aside {
  margin-top: 8px; font-size: 0.8rem; color: var(--rv-text-dim);
  max-width: 42ch; margin-inline: auto;
}

.verdict { padding: 14px 24px 18px; }
.verdict__line { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
/* The sheet says the word; the row cannot spare the width for it and shows the mark alone.
   Its own class rather than the row's, which is now sized for an icon and nothing else. */
.verdict__pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 11px 3px 8px; border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--rv-tone-alert) 42%, transparent);
  background: color-mix(in srgb, var(--rv-tone-alert) 16%, transparent);
  color: color-mix(in srgb, var(--rv-tone-alert) 88%, var(--rv-text));
  font-size: 0.76rem; font-weight: 600; letter-spacing: 0.02em;
}
.verdict__pill .icon { --mdc-icon-size: 14px; width: 14px; height: 14px; }
.verdict__reason { font-size: 0.98rem; line-height: 1.5; }

/*
 * Where this event landed overall, above the chart of what put it there.
 *
 * Same axis, same middle, same two colours as the rows below — it is the same measurement,
 * totalled — so the eye reads the summary and then the parts without re-learning anything.
 *
 * One vertical line in the whole block — the chance line, in the same place as on the chart
 * below. The threshold is a shaded region with no edge drawn on it: a tick read as a second
 * chance line, and even a border on the shading was one more vertical to tell apart. The
 * tint alone says where it starts.
 */
.gauge { margin-top: 18px; }
.gauge__track { position: relative; height: 20px; }
.gauge__track::before {
  content: ""; position: absolute; left: 0; right: 0; top: 5px; height: 10px;
  border-radius: 3px; background: color-mix(in srgb, var(--rv-text) 6%, transparent);
}
/*
 * The track is a scale, not a container: each of its three regions is coloured for what it
 * means, and the event is a mark placed on it.
 *
 * It used to be one grey track with a red bar growing from the middle and a red region at the
 * end, which left a grey stretch between the bar and the region — and that stretch is where
 * most events land. Grey there read as "nothing", when it means "rarer than chance, but not
 * enough to be worth your time", which is the single most common thing this can say.
 *
 * No caption on the regions. The words under the track name the two ends, and the colour runs
 * from one to the other, so a caption would be repeating the axis at it.
 */
.gauge__band {
  position: absolute; top: 5px; height: 10px;
  background: color-mix(in srgb, var(--rv-text) 6%, transparent);
}
.gauge__band[data-band="common"] { left: 0; right: 50%; border-radius: 3px 0 0 3px; }
.gauge__band[data-band="rarer"] {
  background: color-mix(in srgb, var(--rv-tone-alert) 13%, transparent);
}
.gauge__band[data-band="marked"] {
  right: 0; border-radius: 0 3px 3px 0;
  background: color-mix(in srgb, var(--rv-tone-alert) 34%, transparent);
}

/* Where this event landed. A needle rather than a bar: the regions behind it already carry
   the reading, and a filled bar would cover the very region it is being compared against. */
.gauge__needle {
  position: absolute; top: 1px; bottom: 1px; width: 2px; margin-left: -1px;
  border-radius: 2px; background: var(--rv-text);
}
.gauge__needle::after {
  content: ""; position: absolute; left: 50%; top: -3px;
  width: 7px; height: 7px; margin-left: -3.5px;
  border-radius: 50%; background: var(--rv-text);
}
.gauge__needle[data-marked="true"], .gauge__needle[data-marked="true"]::after {
  background: var(--rv-tone-alert);
}
.gauge__zero {
  position: absolute; left: 50%; top: 0; bottom: 0; width: 1px;
  background: color-mix(in srgb, var(--rv-text) 32%, transparent);
}
.gauge__legend {
  position: relative; display: flex; justify-content: space-between;
  gap: 12px; margin-top: 5px;
  font-size: 0.68rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--rv-text-dim);
}

.section {
  padding: 16px 24px 20px;
  border-top: 1px solid var(--rv-line);
}
.section__title {
  font-size: 0.72rem; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--rv-text-dim); margin-bottom: 4px;
}
.section__hint { font-size: 0.78rem; line-height: 1.5; color: var(--rv-text-dim); margin-bottom: 12px; }

/* One template, used by the header row and every term, so "more common / rarer" lands over
   the bars rather than near them. An "auto" last column was the bug: an empty header cell
   is zero wide and a column of figures is not, so the two grids disagreed by 80-odd pixels. */
.term, .axis {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(120px, 34%) 92px;
  gap: 14px;
  align-items: center;
}
.term { padding: 7px 0; }
.term + .term { border-top: 1px solid color-mix(in srgb, var(--rv-line) 60%, transparent); }
.term__what { min-width: 0; }
.term__name { font-size: 0.86rem; font-weight: 600; }
.term__value { font-size: 0.78rem; color: var(--rv-text-dim); overflow-wrap: anywhere; }

/*
 * The centre is zero: "this signal tells me nothing".
 *
 * The track runs the whole width, both sides alike, so the axis sits between two equal
 * fields and reads as the middle of something. It was tinted on the rarer half only, to say
 * which side meant what — and that made the two sides structurally different, so the line
 * read as the edge of the tint rather than as the centre. Which side is which is already
 * said twice, by the labels above and by the colour of the bar itself; a third time was one
 * too many and it cost the thing the chart is measured from.
 *
 * The bar starts a pixel clear of the axis, so a bar on the left does not butt against the
 * line and turn it into a tick that appears to belong to the bar.
 *
 * Thickness and inset are variables because the track and the bar have to agree: they were
 * twenty-two pixels and twelve, which read as the value being thinner than the thing behind
 * it rather than as the same measurement.
 */
.bar {
  --bar-thick: 12px;
  --bar-inset: 5px;
  position: relative;
  height: 22px;
}
.bar::before {
  content: "";
  position: absolute;
  left: 0; right: 0;
  top: var(--bar-inset); height: var(--bar-thick);
  background: color-mix(in srgb, var(--rv-text) 6%, transparent);
  border-radius: 3px;
}
.bar::after {
  content: "";
  position: absolute;
  left: 50%; top: 0; bottom: 0; width: 1px;
  background: color-mix(in srgb, var(--rv-text) 32%, transparent);
}
.bar__fill {
  position: absolute;
  top: var(--bar-inset); height: var(--bar-thick);
  /* A contribution that is small but real still has to be visible; without a floor it
     rounds to nothing and the row looks like it was left out. */
  min-width: 2px;
  border-radius: 3px;
  background: var(--rv-tone-motion);
}
.bar__fill[data-side="rarer"] { left: calc(50% + 1px); background: var(--rv-tone-alert); }
.bar__fill[data-side="common"] { right: calc(50% + 1px); }

.term__figures { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.term__score { font-size: 0.86rem; font-weight: 650; }
.term__score[data-side="rarer"] { color: color-mix(in srgb, var(--rv-tone-alert) 85%, var(--rv-text)); }
.term__seen { font-size: 0.72rem; color: var(--rv-text-dim); }

.axis {
  font-size: 0.68rem; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--rv-text-dim); margin-bottom: 2px;
}
/*
 * The middle is named once, on the gauge, and the word matters.
 *
 * It is not an average — it is the point where a signal is exactly as likely as a coin
 * toss, so it tells you nothing either way. Naming it "chance" also finishes the sentence
 * the other two labels start: more common than chance, rarer than chance.
 *
 * Only on the gauge, which spans the sheet. The chart's own labels sit over a column a
 * third that wide, where a third label in the middle simply lands on the other two — and
 * the two scales share a centre, so saying it once is enough.
 *
 * Placed rather than spaced: with space-between and three items the middle only lands
 * centred when the outer two happen to be the same width, which they are not.
 */
.axis__scale { display: flex; justify-content: space-between; }
.axis__mid {
  position: absolute; left: 50%; transform: translateX(-50%);
  color: color-mix(in srgb, var(--rv-text) 60%, transparent);
}
/*
 * The ends wear the colour of the bars that reach them, so the two colours need no legend
 * of their own — the same mix as the figures in the right-hand column, so a red word and a
 * red number are plainly the same idea. Mixed towards the text rather than the raw tone,
 * which is a fill colour and too light to read at this size.
 */
.axis__rare { color: color-mix(in srgb, var(--rv-tone-alert) 82%, var(--rv-text)); font-weight: 700; }

.note {
  margin: 0; padding: 14px 24px 20px; border-top: 1px solid var(--rv-line);
  font-size: 0.82rem; line-height: 1.55; color: var(--rv-text-dim);
}
.note b { color: var(--rv-text); font-weight: 600; }

.more { font-size: 0.76rem; color: var(--rv-text-dim); padding: 0 24px 4px; }

@media (max-width: 620px) {
  .sheet__head { padding: 16px 16px 0; }
  .sheet__foot { padding: 10px 16px 14px; }
  .verdict, .section { padding-left: 16px; padding-right: 16px; }
  .note { padding-left: 16px; padding-right: 16px; }
  .term { grid-template-columns: minmax(0, 1fr) 92px; row-gap: 6px; }
  /* The bar goes full width under the label rather than being squeezed into a third of a
     phone, where it could no longer show a difference between two contributions. */
  .bar { grid-column: 1 / -1; }
  .axis { display: none; }
}

/*
 * Fixed columns rather than content-sized ones.
 *
 * This group is anchored to the right of the row, so any child that changes width moves
 * every other child with it: "6s" and "3m 12s · 4.1 MB" are forty pixels apart, and that is
 * how far the mark and the play icon slid from one row to the next. Giving the mark its own
 * width was not enough on its own, because the column beside it was still free to change.
 */
.side {
  display: grid;
  grid-template-columns: 78px auto;
  align-items: center;
  gap: 14px;
  flex: 0 0 auto;
}
.facts { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; min-width: 0; }
.facts__main { font-size: 0.86rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.checking { display: flex; align-items: center; gap: 5px; font-size: 0.7rem; color: var(--rv-text-dim); }
.checking .spinner { width: 10px; height: 10px; border-width: 1.5px; }
.go { color: var(--rv-text-dim); }

`;

export class RelevanceSheet extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    adoptStyles(this.shadowRoot, SHARED + STYLES);
    this._built = false;
  }

  set store(store) {
    this._store = store;
  }

  /** Open the sheet for one row. */
  open(event) {
    if (!this._built) this._build();
    this._showDetail(event);
  }

  _build() {
    this._detailHead = el("div", { class: "sheet__headwrap" });
    this._detailBody = el("div", { class: "sheet__scroll" });
    this._detailFoot = el("div", { class: "sheet__foot", hidden: true });

    this._detail = el(
      "dialog",
      {
        class: "sheet",
        // showModal focuses the first focusable child, which would open the sheet with a ring
        // around its close button. The dialog takes focus itself instead.
        tabindex: "-1",
        autofocus: true,
        // Left and right page through a segment that holds several detections, because
        // reaching for a footer button to compare two of them is the wrong amount of work.
        onkeydown: (keyEvent) => {
          if (keyEvent.key === "ArrowLeft") this._page(-1);
          if (keyEvent.key === "ArrowRight") this._page(1);
        },
      },
      this._detailHead,
      this._detailBody,
      this._detailFoot
    );

    this.shadowRoot.append(this._detail);
    this._built = true;
  }

  _showDetail(event) {
    const store = this._store;
    this._sheet = {
      event,
      known: store.relevanceFor(event.entry_id, event.channel),
      found: store.eventRelevance(event),
      at: 0,
    };

    this._detailHead.replaceChildren(
      el(
        "div",
        { class: "sheet__head" },
        el(
          "div",
          {},
          el("div", { class: "sheet__when", text: formatDayLabel(event.start.slice(0, 10)) }),
          el("div", {
            class: "sheet__title",
            text: `${formatTime(event.start)} · ${event.camera}`,
          })
        ),
        el(
          "button",
          { class: "sheet__close", "aria-label": "Close", onclick: () => this._detail.close() },
          icon("mdi:close")
        )
      )
    );

    this._renderDetail();
    this._detail.showModal();
  }

  /** Move to another detection inside the same recording, if there is one. */
  _page(step) {
    const sheet = this._sheet;
    if (!sheet || sheet.found.length < 2) return;
    const next = sheet.at + step;
    if (next < 0 || next >= sheet.found.length) return;
    sheet.at = next;
    this._renderDetail();
    this._detailBody.scrollTop = 0;
  }

  /** Draw whichever detection is being looked at, and the footer that moves between them. */
  _renderDetail() {
    const { known, found, at } = this._sheet;

    this._detailBody.replaceChildren();
    if (!known) {
      this._detailBody.append(
        el("p", { class: "note", text: "Learning what is normal is switched off." })
      );
    } else if (found.length === 0) {
      // A header and one grey line in a 680px sheet reads as a modal that failed to load.
      // The state is legitimate — a stretch of footage the journal has nothing for, or a
      // camera still collecting — and it should look like an answer rather than an absence.
      this._detailBody.append(
        el(
          "div",
          { class: "blank" },
          icon("mdi:timeline-text-outline"),
          el("p", { class: "blank__body", html: this._collectingNote(known) }),
          el("p", {
            class: "blank__aside",
            text:
              "Detections are counted as the sensors report them, which is not always the " +
              "same as what the recorder tagged this recording with.",
          })
        )
      );
    } else {
      this._detailBody.append(...this._detailFor(found[at], known));
    }

    // Only when there is somewhere to go: one detection needs no footer saying so.
    this._detailFoot.hidden = found.length < 2;
    if (found.length < 2) return;

    this._detailFoot.replaceChildren(
      el("span", {
        class: "sheet__count",
        text: `Detection ${at + 1} of ${found.length} in this recording`,
      }),
      el(
        "div",
        { class: "sheet__pager" },
        el(
          "button",
          { class: "btn btn--quiet", disabled: at === 0, onclick: () => this._page(-1) },
          icon("mdi:chevron-left"),
          "Previous"
        ),
        el(
          "button",
          {
            class: "btn btn--quiet",
            disabled: at === found.length - 1,
            onclick: () => this._page(1),
          },
          "Next",
          icon("mdi:chevron-right")
        )
      )
    );
  }

  /**
   * What the panel says while a camera has too little behind it to compare anything.
   *
   * Names the requirement that is actually outstanding. It used to recite both — "a
   * fortnight, and a few hundred detections" — at a camera that had four hundred and ninety
   * three of them and was short only of the calendar, which reads as the panel not knowing
   * what it is waiting for.
   */
  _collectingNote(known) {
    const seen = known.coverage || { days: 0, events: 0 };
    const needs = known.needs || null;
    if (!needs) {
      return `Still collecting: <b>${seen.days} days</b> and <b>${seen.events} detections</b> so far.`;
    }

    const shortOfDays = seen.days < needs.days;
    const shortOfEvents = seen.events < needs.events;

    if (shortOfDays && shortOfEvents) {
      return (
        `Still collecting: <b>${seen.days} of ${needs.days} days</b> and ` +
        `<b>${seen.events} of ${needs.events} detections</b>.`
      );
    }
    if (shortOfDays) {
      return (
        `Still collecting: <b>${seen.days} of ${needs.days} days</b>. It has seen ` +
        `${seen.events} detections, which is already enough — a camera needs a full week ` +
        `as well, because a Saturday does not look like a Tuesday.`
      );
    }
    if (shortOfEvents) {
      return (
        `This camera has been watched for <b>${seen.days} days</b> but has seen only ` +
        `<b>${seen.events} of the ${needs.events} detections</b> it needs to be compared ` +
        `against itself.`
      );
    }
    return `Nothing was recorded inside this stretch of footage.`;
  }

  /**
   * One detection, as a verdict and the evidence for it.
   *
   * The bars are scaled to the largest contribution in *this* detection rather than to a
   * fixed range: what the reader wants is which signal mattered most here, and a fixed scale
   * would flatten every ordinary event into four invisible stubs.
   */
  _detailFor(item, known) {
    const names = {
      clock: "Time of day",
      solar: "Light",
      duration: "How long it lasted",
      predecessor: "What came first",
    };
    const widest = Math.max(...item.terms.map((term) => Math.abs(term.contribution)), 0.5);

    const verdict = el(
      "div",
      { class: "verdict" },
      el(
        "div",
        { class: "verdict__line" },
        item.unusual
          ? el("span", { class: "verdict__pill" }, icon("mdi:circle-slice-8"), "Unusual")
          : null,
        // Through the same lookup the rows use, so "vehicle" reads "Vehicle" here too and
        // carries the same colour and icon it does in the list.
        (() => {
          const meta = triggerMeta(item.kind);
          return el(
            "span",
            { class: "chip", dataset: { tone: meta.tone } },
            icon(meta.icon),
            meta.label
          );
        })()
      ),
      el("div", { class: "verdict__reason", text: item.reason })
    );

    // Always drawn, threshold or not.
    //
    // It used to be skipped while a camera was still collecting, which is every camera on a
    // fresh install — so the summary simply vanished and the sheet showed one chart instead
    // of two. That made a new install look like a different build from one with history in
    // it, and it contradicted the one thing this view is for: answering from the first day.
    //
    // Without a threshold there is no region to shade and nothing can be marked, so the
    // scale still says where the event sits and stops short of claiming a verdict.
    const hasCut = item.threshold !== null && item.threshold !== undefined;
    const extent =
      Math.max(Math.abs(item.score), hasCut ? Math.abs(item.threshold) : 0, 1) * 1.15;
    // Where the marked region begins, as a share of the track. Without a threshold there is
    // nothing entitled to be marked, so the rarer region simply runs to the end.
    const cut = hasCut
      ? Math.max(50, Math.min(100, 50 + (item.threshold / extent) * 50))
      : 100;
    verdict.append(
      el(
        "div",
        { class: "gauge" },
        el(
          "div",
          { class: "gauge__track" },
          // The three regions, then the chance line, then the event on top of both.
          el("div", { class: "gauge__band", dataset: { band: "common" } }),
          el("div", {
            class: "gauge__band",
            dataset: { band: "rarer" },
            style: { left: "50%", right: `${100 - cut}%` },
          }),
          hasCut
            ? el("div", {
                class: "gauge__band",
                dataset: { band: "marked" },
                style: { left: `${cut}%` },
                title: `Marked above ${item.threshold}`,
              })
            : null,
          el("div", { class: "gauge__zero" }),
          el("div", {
            class: "gauge__needle",
            dataset: { marked: String(Boolean(item.unusual)) },
            style: { left: `${Math.max(1, Math.min(99, 50 + (item.score / extent) * 50))}%` },
            title: `This event scored ${item.score}`,
          })
        ),
        el(
          "div",
          { class: "gauge__legend" },
          el("span", { text: "more common" }),
          el("span", { class: "axis__mid", text: "chance" }),
          // "Unusual" is a verdict, and without a threshold there is nothing entitled to
          // reach one — so the end goes back to naming the direction.
          el("span", { class: "axis__rare", text: hasCut ? "unusual" : "rarer" })
        )
      )
    );

    const evidence = el(
      "div",
      { class: "section" },
      el("div", { class: "section__title", text: "What went into it" }),
      el("div", {
        class: "section__hint",
        text:
          "A signal on the line told us nothing. The further out it reaches, the more it " +
          "counted — left if it is what this camera usually sees, right if it is not.",
      }),
      el(
        "div",
        { class: "axis" },
        el("span", {}),
        el(
          "span",
          { class: "axis__scale" },
          el("span", { text: "more common" }),
          el("span", { class: "axis__rare", text: "rarer" })
        ),
        el("span", {})
      )
    );

    // Rarest first, commonest last. The order used to be the order the scorer happens to
    // build them in — clock, sun, duration, and so on — which buries the one signal that made
    // this event stand out somewhere in the middle of five that did not. Sorted, the answer
    // to "why is this marked" is the first line, and the bars descend from it.
    const ordered = [...item.terms].sort((left, right) => right.contribution - left.contribution);

    for (const term of ordered) {
      const rarer = term.contribution > 0;
      const width = (Math.abs(term.contribution) / widest) * 48;
      evidence.append(
        el(
          "div",
          { class: "term" },
          el(
            "div",
            { class: "term__what" },
            // A chosen signal names itself; the built-in terms are named by the panel.
            el("div", {
              class: "term__name",
              text: term.subject || names[term.name] || term.name,
            }),
            el("div", { class: "term__value", text: term.label })
          ),
          el(
            "div",
            { class: "bar" },
            el("div", {
              class: "bar__fill",
              dataset: { side: rarer ? "rarer" : "common" },
              style: { width: `${width}%` },
            })
          ),
          el(
            "div",
            { class: "term__figures" },
            el("div", {
              class: "term__score",
              dataset: { side: rarer ? "rarer" : "common" },
              text: `${rarer ? "+" : ""}${term.contribution}`,
            }),
            el("div", { class: "term__seen", text: `seen ${term.seen}×` })
          )
        )
      );
    }

    const blocks = [verdict, evidence];
    if (item.threshold === null || item.threshold === undefined) {
      blocks.push(el("p", { class: "note", html: this._collectingNote(known) }));
    }
    return blocks;
  }

  /** A span that behaves like a button, because the row itself already is one.
   *
   * `onOpen` is pulled out rather than spread through: `el` turns any `on*` function into an
   * event listener, so leaving it in would quietly register one for an "open" event that
   * never fires.
   */
}

if (!customElements.get("reolink-relevance-sheet")) {
  customElements.define("reolink-relevance-sheet", RelevanceSheet);
}
