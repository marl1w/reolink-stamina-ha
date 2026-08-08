/**
 * Tests for when the panel introduces itself, run under node.
 *
 * A dialog that opens when it should not is the most annoying thing a panel can do, and the
 * rule that governs it is four lines with three states that are easy to confuse: never seen
 * anything, seen an older release, seen this one. Getting it wrong means either nagging
 * every visit or never appearing at all, and neither shows up in a screenshot.
 *
 * Run via scripts/check.sh.
 */

import assert from "node:assert/strict";

import {
  FEATURES,
  shouldIntroduce,
  summarise,
} from "../../custom_components/reolink_stamina/frontend/whats-new.js";

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

test("a fresh install is left alone", () => {
  // Somebody who just chose to install this read something about it five minutes ago.
  assert.equal(shouldIntroduce(null, "1.3.0", false), false);
});

test("an upgrade from before this existed is introduced", () => {
  assert.equal(shouldIntroduce(null, "1.3.0", true), true);
});

test("an upgrade from an earlier release is introduced", () => {
  assert.equal(shouldIntroduce("1.2.12", "1.3.0", true), true);
});

test("having seen this release, it stays gone", () => {
  assert.equal(shouldIntroduce("1.3.0", "1.3.0", true), false);
});

test("a downgrade counts as a change rather than being ignored", () => {
  // Deliberately not a version comparison: "different from what you were last shown" is the
  // question, and it needs no opinion about which way round two version strings go.
  assert.equal(shouldIntroduce("1.3.0", "1.2.12", true), true);
});

test("an unknown version says nothing at all", () => {
  // Better silent than opening on every single load because there is nothing to remember.
  assert.equal(shouldIntroduce("1.3.0", undefined, true), false);
  assert.equal(shouldIntroduce(null, "", true), false);
});

test("every feature says what it is and where to find it", () => {
  assert.ok(FEATURES.length >= 3, "the panel has more than one feature to introduce");
  for (const feature of FEATURES) {
    assert.ok(feature.title, "a feature with no title");
    assert.ok(feature.text.length > 40, `${feature.title}: too terse to be useful`);
    assert.ok(feature.where, `${feature.title}: nothing says where to find it`);
    assert.ok(feature.icon.startsWith("mdi:"), `${feature.title}: no icon`);
  }
});

test("the betas are marked as such", () => {
  // "Why don't I have that?" is the question this dialog exists to prevent, not cause.
  const betas = FEATURES.filter((feature) => feature.beta).map((feature) => feature.title);
  assert.ok(
    betas.some((title) => title.toLowerCase().includes("normal")),
    "learning what is normal is a beta and must say so"
  );
});

test("the summary counts the list rather than claiming a number", () => {
  // It said "three of these are here already" over a list of two, because the list gained an
  // entry and the sentence did not. Counted now, and checked here so it cannot drift again.
  const beta = FEATURES.filter((feature) => feature.beta).length;
  const words = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight"];
  const line = summarise();

  assert.ok(
    line.toLowerCase().startsWith(`${words[FEATURES.length - beta]} of these are ready`) ||
      line.startsWith("One of these is ready"),
    `the ready count is wrong: ${line}`
  );
  assert.ok(line.includes(words[beta]), `the beta count is wrong: ${line}`);
});

test("the summary copes with a list that is all one thing", () => {
  const stable = [{ beta: false }, { beta: false }];
  assert.ok(!summarise(stable).includes("beta"), "nothing in beta, so say nothing about it");
  assert.ok(summarise([{ beta: true }]).includes("the one marked beta is off"));
});

process.stdout.write(`\n  ${ran - failures}/${ran} what's-new checks passed\n`);
process.exit(failures > 0 ? 1 : 0);
