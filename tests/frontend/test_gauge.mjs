/**
 * Tests for the scale the relevance sheet draws one event's score on, run under node.
 *
 * These exist because of a bug no screenshot would have caught. The extent was derived from
 * the score being plotted, so it cancelled: every event past its camera's threshold put the
 * needle in exactly the same place, and the shaded threshold region moved instead. The sheet
 * looked plausible on any single event and told the same story about all of them.
 *
 * So what is pinned here is not a pixel but the property that was missing — that the scale is
 * shared across the window, and therefore that two different scores draw two different
 * pictures. The live figures below are from a real installation, nine cameras and a fortnight.
 *
 * Run via scripts/check.sh.
 */

import assert from "node:assert/strict";

import {
  HEADROOM,
  cutAt,
  extentFor,
  needleAt,
} from "../../custom_components/reolink_stamina/frontend/gauge.js";

let failures = 0;
let ran = 0;

function test(name, fn) {
  ran += 1;
  try {
    fn();
    process.stdout.write(`  ok ${name}\n`);
  } catch (error) {
    failures += 1;
    process.stdout.write(`  FAILED ${name}\n    ${error.message}\n`);
  }
}

/** Where each score in a window lands, as the sheet would draw them. */
const spread = (scores, threshold) => {
  const extent = extentFor(scores, threshold);
  return scores.map((score) => needleAt(score, extent));
};

// One camera's window, measured on the installation this was found on: scores from -10.42 to
// -2.55, marked above 1.4. Under the old arithmetic every one of these drew the needle at
// 6.52%.
const BALCONE = [-10.42, -10.05, -9.76, -9.38, -9.05, -4.31, -2.55];

test("two very different scores do not draw the same picture", () => {
  const [mild, extreme] = spread([-0.8, -10.4], 1.4);
  assert.notEqual(mild, extreme);
  assert.ok(mild > extreme, `${mild} should sit right of ${extreme}`);
});

test("a whole window of scores lands in a whole window of places", () => {
  const places = spread(BALCONE, 1.4);
  assert.equal(new Set(places).size, BALCONE.length);
});

test("rarer is always further right", () => {
  const extent = extentFor(BALCONE, 1.4);
  const ordered = [...BALCONE].sort((a, b) => a - b);
  const places = ordered.map((score) => needleAt(score, extent));
  for (let at = 1; at < places.length; at += 1) {
    assert.ok(places[at] > places[at - 1], `${ordered[at]} should sit right of ${ordered[at - 1]}`);
  }
});

test("the threshold region sits in the same place for every event in the window", () => {
  // The half of the bug that made the axis meaningless: the shading used to be measured
  // against the extent of whichever event was open, so it moved as you paged between them.
  const extent = extentFor(BALCONE, 1.4);
  const cuts = new Set(BALCONE.map(() => cutAt(1.4, extent)));
  assert.equal(cuts.size, 1);
});

test("chance is the middle", () => {
  assert.equal(needleAt(0, extentFor([0, -3, 2], 1.4)), 50);
});

test("the widest score in the window stays on the track", () => {
  const extent = extentFor(BALCONE, 1.4);
  const place = needleAt(-10.42, extent);
  assert.ok(place > 1, "the rarest event should not be clamped to the very end");
  assert.ok(place < 50, "a negative score belongs on the common half");
  // Exactly the headroom it was given, and no more.
  assert.equal(Number(place.toFixed(2)), Number((50 - 50 / HEADROOM).toFixed(2)));
});

test("nothing ever leaves the track", () => {
  for (const score of [-500, -10.42, 0, 4.67, 500]) {
    const place = needleAt(score, extentFor(BALCONE, 1.4));
    assert.ok(place >= 1 && place <= 99, `${score} landed at ${place}`);
  }
});

test("a threshold above everything in the window is still on the track", () => {
  // A quiet fortnight under a strict floor: nothing came close to being marked, and the
  // region saying where "marked" begins still has to be visible, or the sheet claims the
  // camera can never mark anything.
  const cut = cutAt(2.68, extentFor([-1.2, -0.4, 0.3], 2.68));
  assert.ok(cut > 50 && cut < 100, `expected a visible region, got ${cut}`);
});

test("no threshold shades nothing", () => {
  // A camera still collecting. There is no verdict to draw, and the region has no width.
  assert.equal(cutAt(null, extentFor([-2, 1], null)), 100);
  assert.equal(cutAt(undefined, extentFor([-2, 1], undefined)), 100);
});

test("a negative threshold never shades the common half", () => {
  // Real: per-camera quantiles ran as low as -0.63 before the absolute floor was added.
  assert.equal(cutAt(-0.63, extentFor([-2, 1], -0.63)), 50);
});

test("a window of near-zero scores is not magnified into a full track", () => {
  // Without a floor on the extent, three scores within a rounding error of chance would each
  // be flung to an end of the track and read as a verdict.
  const places = spread([0.01, -0.02, 0.03], null);
  for (const place of places) {
    assert.ok(Math.abs(place - 50) < 5, `${place} is too far from chance to be drawn as chance`);
  }
});

test("an empty window still gives a usable scale", () => {
  // The sheet draws the gauge before it has anything to put on it on a fresh install.
  assert.ok(extentFor([], null) > 0);
  assert.equal(needleAt(0, extentFor([], null)), 50);
});

test("a missing score is not a zero-width track", () => {
  // The payload rounds to two places and has always sent a number, but a scale that divides
  // by whatever it was handed should not be the thing that breaks if that ever changes.
  assert.ok(extentFor([undefined, null], null) > 0);
});

process.stdout.write(`\n  ${ran - failures}/${ran} gauge checks passed\n`);
process.exit(failures === 0 ? 0 : 1);
