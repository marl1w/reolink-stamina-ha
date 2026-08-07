/**
 * Tests for the playback ladder and its memory, run under node.
 *
 * Both are decisions rather than drawing, and both are easy to get subtly wrong in ways a
 * browser would only show as "playback got slower for everyone": a ladder that skips the
 * cheap rung, or a memory that pins one camera's answer onto every other camera.
 *
 * `routes.js` probes the browser at import time; under node those probes fail and are
 * caught, which is exactly the fallback path this exercises.
 *
 * Run via scripts/check.sh.
 */

import assert from "node:assert/strict";

import {
  CONVERTED_ROUTES,
  ROUTE_LABELS,
  ROUTE_REMUX,
  ROUTE_STREAM,
  ROUTE_TRANSCODE,
  nextRoute,
  recalledRoute,
  rememberRoute,
  routeMemoryKeys,
} from "../../custom_components/reolink_stamina/frontend/playback/routes.js";

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

const EVENT = { entry_id: "nvr1", channel: 3 };
const NOW = 1_700_000_000_000;

// ------------------------------------------------------------------- the ladder

test("with the beta off there is nothing to fall back to", () => {
  // The panel used to hand over the recorder's whole file here. It was unseekable, minutes
  // long and in the codec that had just failed; the player now offers the download instead.
  assert.equal(nextRoute(ROUTE_STREAM, { adaptive: false }), null);
  assert.equal(nextRoute(ROUTE_STREAM, { adaptive: false, decodeFailure: true }), null);
});

test("the cheap rung comes first, and the ladder ends after the re-encode", () => {
  assert.equal(nextRoute(ROUTE_STREAM, { adaptive: true }), ROUTE_REMUX);
  assert.equal(nextRoute(ROUTE_REMUX, { adaptive: true }), ROUTE_TRANSCODE);
  assert.equal(nextRoute(ROUTE_TRANSCODE, { adaptive: true }), null);
});

test("a refused codec skips repackaging, which would hand it the same codec", () => {
  // Repackaging changes the container and leaves the bitstream alone, so on a browser with
  // one way into a decoder it cannot help one that has already refused this bitstream.
  // Under node the HLS probe fails, which is the shape of Chrome and Firefox.
  assert.equal(nextRoute(ROUTE_STREAM, { adaptive: true, decodeFailure: true }), ROUTE_TRANSCODE);
});

test("a container this browser could not read is exactly what repackaging is for", () => {
  // An iPhone has no Media Source Extensions, so nothing demuxed the recording and its own
  // decoder was never asked. Re-encoding there would be pure waste.
  assert.equal(nextRoute(ROUTE_STREAM, { adaptive: true, decodeFailure: false }), ROUTE_REMUX);
});

test("every route can name itself, the cheap one included", () => {
  // The player badges whichever route a clip arrived on, so a missing label is a badge that
  // silently disappears — which is how "Direct play" went unsaid for a whole release.
  for (const route of [ROUTE_STREAM, ROUTE_REMUX, ROUTE_TRANSCODE]) {
    const label = ROUTE_LABELS[route];
    assert.ok(label?.icon && label?.badge && label?.hint, `${route} cannot describe itself`);
  }
});

test("only the conversions count as converted", () => {
  assert.ok(CONVERTED_ROUTES.has(ROUTE_REMUX) && CONVERTED_ROUTES.has(ROUTE_TRANSCODE));
  assert.ok(!CONVERTED_ROUTES.has(ROUTE_STREAM));
});

// ------------------------------------------------------------------- the memory

test("nothing remembered means the cheapest route", () => {
  assert.equal(recalledRoute({}, routeMemoryKeys(EVENT, "sub"), NOW), null);
});

test("what worked for one camera is the opening guess for the next", () => {
  const memory = rememberRoute({}, routeMemoryKeys(EVENT, "main"), ROUTE_TRANSCODE, NOW);

  const other = { entry_id: "nvr1", channel: 7 };
  assert.equal(recalledRoute(memory, routeMemoryKeys(other, "main"), NOW), ROUTE_TRANSCODE);
  // Only for the resolution it was learned on: the sub stream is usually H.264.
  assert.equal(recalledRoute(memory, routeMemoryKeys(other, "sub"), NOW), null);
});

test("a camera that plays natively is not converted because another one needed it", () => {
  const keys = routeMemoryKeys(EVENT, "main");
  let memory = rememberRoute({}, keys, ROUTE_TRANSCODE, NOW);

  const fine = { entry_id: "nvr1", channel: 7 };
  memory = rememberRoute(memory, routeMemoryKeys(fine, "main"), ROUTE_STREAM, NOW);

  assert.equal(recalledRoute(memory, routeMemoryKeys(fine, "main"), NOW), ROUTE_STREAM);
  // And the hint the other camera left is still there for cameras never seen before.
  assert.equal(recalledRoute(memory, keys, NOW), ROUTE_TRANSCODE);
});

test("passthrough working is recorded, not merely the absence of a conversion", () => {
  const keys = routeMemoryKeys(EVENT, "sub");
  const memory = rememberRoute({}, keys, ROUTE_STREAM, NOW);
  assert.equal(memory[keys.camera].route, ROUTE_STREAM);
  // A resolution hint is only ever written for a conversion.
  assert.equal(memory[keys.hint], undefined);
});

test("an old answer is re-examined rather than trusted for ever", () => {
  const keys = routeMemoryKeys(EVENT, "main");
  const memory = rememberRoute({}, keys, ROUTE_TRANSCODE, NOW);
  const later = NOW + 91 * 24 * 3600 * 1000;
  assert.equal(recalledRoute(memory, keys, later), null);
});

test("the memory cannot grow without bound, and never drops the hints", () => {
  let memory = {};
  for (let channel = 0; channel < 260; channel += 1) {
    memory = rememberRoute(
      memory,
      routeMemoryKeys({ entry_id: "nvr1", channel }, "main"),
      ROUTE_REMUX,
      NOW + channel
    );
  }
  const cameras = Object.keys(memory).filter((key) => !key.startsWith("*|"));
  assert.ok(cameras.length <= 200, `kept ${cameras.length} camera entries`);
  assert.equal(memory["*|main"].route, ROUTE_REMUX);
  // The oldest went first.
  assert.equal(memory["nvr1|0|main"], undefined);
  assert.equal(memory["nvr1|259|main"].route, ROUTE_REMUX);
});

// ------------------------------------------------- the same ladder, seen from Safari

// `routes.js` asks the browser what it can play at import time, so a browser that answers
// differently has to be a different import. The query string is what gets past node's module
// cache; `document` has to exist before the import, not before the assertion.
globalThis.document = {
  createElement: () => ({
    canPlayType: (type) => (type === "application/vnd.apple.mpegurl" ? "maybe" : ""),
  }),
};
const safari = await import("../../custom_components/reolink_stamina/frontend/playback/routes.js?safari");
delete globalThis.document;

test("a browser with its own HLS pipeline repackages before it re-encodes", () => {
  // The expensive mistake this is here to prevent. Safari stalls on H.265 through Media
  // Source Extensions and plays it perfectly through native HLS, so a decode failure there
  // says nothing about the second pipeline — and skipping to a re-encode sent every
  // high-resolution clip through the costliest route on the machine, for no reason.
  assert.equal(safari.NATIVE_HLS, true);
  assert.equal(
    safari.nextRoute(ROUTE_STREAM, { adaptive: true, decodeFailure: true }),
    ROUTE_REMUX
  );
});

test("repackaging failing there still leads to the re-encode", () => {
  // The rung below is unchanged: if the native pipeline will not take it either, the codec
  // really is the problem and only re-encoding is left.
  assert.equal(safari.nextRoute(ROUTE_REMUX, { adaptive: true }), ROUTE_TRANSCODE);
  assert.equal(safari.nextRoute(ROUTE_TRANSCODE, { adaptive: true, decodeFailure: true }), null);
});

process.stdout.write(`\n  ${ran - failures}/${ran} route checks passed\n`);
process.exit(failures > 0 ? 1 : 0);
