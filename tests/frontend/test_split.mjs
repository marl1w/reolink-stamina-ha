/**
 * Tests for how the panel divides itself between the list and the player, run under node.
 *
 * The two things worth pinning down are the ones a browser would only show you by being the
 * wrong shape: that a panel too narrow or too tall for a split does not get one, and that no
 * drag of the divider can take the rows below the width a row needs. The numbers are the
 * whole of the behaviour, so they are checked here rather than by eye at some window size.
 *
 * Run via scripts/check.sh.
 */

import assert from "node:assert/strict";

import {
  GUTTER,
  LIST_MIN,
  PLAYER_MIN,
  SPLIT_MIN_WIDTH,
  clampPlayerWidth,
  openingPlayerWidth,
  splitFits,
} from "../../custom_components/reolink_stamina/frontend/split.js";

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

/** The space the panes share at a given panel width, as the panel computes it. */
const shared = (panelWidth) => panelWidth - GUTTER;

test("a wide desktop panel is split", () => {
  assert.equal(splitFits(1600, 900), true);
});

test("a phone is not", () => {
  assert.equal(splitFits(390, 844), false);
});

test("neither is a small desktop window", () => {
  assert.equal(splitFits(820, 700), false);
});

test("nor a portrait window, however wide", () => {
  // A rotated monitor: room for both by width alone, but the shape where neither would be
  // usable side by side.
  assert.equal(splitFits(1200, 1600), false);
});

test("a landscape phone is short, not narrow, and stays whole", () => {
  assert.equal(splitFits(844, 390), false);
});

test("the split threshold leaves both minimums room", () => {
  assert.ok(
    LIST_MIN + PLAYER_MIN <= shared(SPLIT_MIN_WIDTH),
    `a split at ${SPLIT_MIN_WIDTH} cannot hold ${LIST_MIN} + ${PLAYER_MIN} + ${GUTTER}`
  );
});

test("dragging the divider all the way over still leaves the rows a list", () => {
  const total = shared(1440);
  const width = clampPlayerWidth(99_999, total);
  assert.equal(total - width, LIST_MIN);
});

test("and dragging it the other way leaves a player, not a strip", () => {
  assert.equal(clampPlayerWidth(0, shared(1440)), PLAYER_MIN);
});

test("a width that fits is honoured as asked", () => {
  assert.equal(clampPlayerWidth(700, shared(1600)), 700);
});

test("a remembered width survives a window that has no room for it", () => {
  // The panel keeps the asked-for width and re-clamps at every size, so a session in a
  // narrow window must not be able to shrink the number it goes back to.
  const asked = 900;
  const narrow = clampPlayerWidth(asked, shared(1000));
  assert.ok(narrow < asked, "the narrow window should have squeezed it");
  assert.equal(clampPlayerWidth(asked, shared(1800)), asked);
});

test("the smallest split gives both panes their minimum and no more", () => {
  const total = shared(SPLIT_MIN_WIDTH);
  const width = clampPlayerWidth(99_999, total);
  assert.ok(width >= PLAYER_MIN);
  assert.ok(total - width >= LIST_MIN);
});

test("the opening width is a share of the panel", () => {
  const total = shared(1440);
  const width = openingPlayerWidth(total);
  assert.ok(width > PLAYER_MIN, "and not the floor");
  assert.ok(total - width > LIST_MIN, "and not the ceiling");
});

test("the opening width never runs away with a very wide screen", () => {
  // Half of a 5K panel would be a video the height of the window and nothing to browse.
  assert.ok(openingPlayerWidth(shared(5120)) <= 800);
});

test("the opening width fits the smallest split there is", () => {
  const total = shared(SPLIT_MIN_WIDTH);
  const width = openingPlayerWidth(total);
  assert.ok(width >= PLAYER_MIN, `${width} is below the player's own floor`);
  assert.ok(total - width >= LIST_MIN, `${total - width} leaves the rows too little`);
});

process.stdout.write(`\n  ${ran - failures}/${ran} split checks passed\n`);
process.exit(failures > 0 ? 1 : 0);
