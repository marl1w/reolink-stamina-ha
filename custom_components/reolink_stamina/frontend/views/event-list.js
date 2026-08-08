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
.odd {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--rv-tone-alert) 42%, transparent);
  background: color-mix(in srgb, var(--rv-tone-alert) 16%, transparent);
  color: color-mix(in srgb, var(--rv-tone-alert) 88%, var(--rv-text));
  cursor: pointer;
  flex: 0 0 auto;
}
.odd:hover { background: color-mix(in srgb, var(--rv-tone-alert) 26%, transparent); }
/* Blocked, not inline: an inline icon sits on the text baseline, which inside a pill
   with no text puts it a pixel or two below the middle of its own background. */
.odd .icon { --mdc-icon-size: 14px; width: 14px; height: 14px; display: block; }

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
.why {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  background: none;
  padding: 4px;
  border-radius: 50%;
  color: color-mix(in srgb, var(--rv-text-dim) 75%, transparent);
  cursor: pointer;
  transition: color 120ms var(--rv-ease);
  flex: 0 0 auto;
}
.why:hover, .why:focus-visible { color: var(--rv-text); }
.why .icon { --mdc-icon-size: 14px; width: 14px; height: 14px; display: block; }

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
    this._detailHead = el("div");
    this._detailBody = el("div", { class: "sheet__scroll" });
    this._detailFoot = el("div", { class: "sheet__foot", hidden: true });
    this._detail = el(
      "dialog",
      {
        class: "sheet",
        // `showModal` focuses the first focusable thing it finds, which is the close button,
        // and a pointer-opened dialog then opens with a focus ring around its X. Taking focus
        // on the dialog itself leaves the ring for whoever tabs to the button on purpose.
        tabindex: "-1",
        autofocus: true,
        // Left and right page through a segment that holds several detections, because
        // reaching for a footer button to compare two of them is the wrong amount of work.
        onkeydown: (event) => {
          if (event.key === "ArrowLeft") this._page(-1);
          if (event.key === "ArrowRight") this._page(1);
        },
      },
      this._detailHead,
      this._detailBody,
      this._detailFoot
    );
    this.shadowRoot.append(
      el("div", {}, this._notices, this._stateHost, this._list, this._detail)
    );
    this._built = true;
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

    for (const term of item.terms) {
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
                onOpen: () => this._showDetail(event),
              },
              icon("mdi:circle-slice-8")
            )
          : this._pressable(
              {
                class: "why",
                title: "What has been learned about this",
                "aria-label": "What has been learned about this event",
                onOpen: () => this._showDetail(event),
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
