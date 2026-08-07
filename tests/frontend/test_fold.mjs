/**
 * Tests for the folding toolbar's scroll heuristic, run under node.
 *
 * A decision made from a stream of scroll positions, which is exactly the shape of thing
 * that looks fine in a browser and then misbehaves under a real thumb — a toolbar that
 * flaps under a resting finger, or one that drops in over the row somebody was reading.
 * The clock is injected, so a flick and a drift can be told apart without waiting for one.
 *
 * Run via scripts/check.sh.
 */

import assert from "node:assert/strict";

import { ToolbarFold } from "../../custom_components/reolink_stamina/frontend/fold.js";

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

/** A fold with a clock under our control, plus a `scroll(to, overMs)` in place of a thumb. */
function harness() {
  let clock = 0;
  const fold = new ToolbarFold({ now: () => clock });
  /** Move to `top`, delivered as `steps` scroll events spread over `overMs`. */
  const scroll = (top, overMs = 100, steps = 5) => {
    const from = fold._top;
    let answer = fold.collapsed;
    for (let step = 1; step <= steps; step += 1) {
      clock += overMs / steps;
      answer = fold.update(from + ((top - from) * step) / steps);
    }
    return answer;
  };
  const wait = (ms) => {
    clock += ms;
  };
  return { fold, scroll, wait };
}

test("the filters stay put while the list is still near the top", () => {
  const { scroll } = harness();
  assert.equal(scroll(60, 200), false);
});

test("reading down the list folds them away", () => {
  const { scroll } = harness();
  assert.equal(scroll(500, 400), true);
});

test("a slow drift back up leaves them folded", () => {
  const { scroll } = harness();
  scroll(900, 600);
  // Two hundred pixels, but taken over four seconds: someone re-reading a row, not someone
  // reaching for the filters.
  assert.equal(scroll(700, 4000, 20), true);
});

test("a short flick up is not enough on its own", () => {
  const { scroll, wait } = harness();
  scroll(900, 600);
  wait(400);
  // Fast, but only eighty pixels of it.
  assert.equal(scroll(820, 100), true);
});

test("a long fast flick up brings them back", () => {
  const { scroll, wait } = harness();
  scroll(900, 600);
  wait(400);
  assert.equal(scroll(600, 200), false);
});

test("nudges up separated by pauses never add up to a flick", () => {
  const { fold, scroll, wait } = harness();
  scroll(1200, 800);
  let top = 1200;
  for (let nudge = 0; nudge < 8; nudge += 1) {
    top -= 60;
    scroll(top, 60, 3);
    wait(500); // thumb lifted between each one
  }
  assert.equal(fold.collapsed, true, "eight separate nudges should not read as one flick");
  assert.ok(top > 64, "and the list should still be well away from the top");
});

test("reaching the top brings them back however it was reached", () => {
  const { scroll } = harness();
  scroll(900, 600);
  assert.equal(scroll(0, 8000, 40), false);
});

test("a resting thumb does not make the toolbar flap", () => {
  const { fold, scroll } = harness();
  scroll(600, 400);
  // Jitter of a couple of pixels either way, which is what a still finger on a touch screen
  // actually reports.
  for (const [top, ms] of [[602, 40], [599, 40], [601, 40], [598, 40], [600, 40]]) {
    assert.equal(scroll(top, ms, 1), true);
  }
  assert.equal(fold.collapsed, true);
});

test("changing direction starts the run over", () => {
  const { scroll, wait } = harness();
  scroll(900, 600);
  wait(400);
  // Most of a qualifying flick up, then back down — the upward distance must not be waiting
  // there to be topped up by a later one.
  scroll(780, 90, 3);
  scroll(820, 60, 2);
  wait(400);
  assert.equal(scroll(770, 60, 2), true);
});

process.stdout.write(`\n  ${ran - failures}/${ran} fold checks passed\n`);
process.exit(failures > 0 ? 1 : 0);
