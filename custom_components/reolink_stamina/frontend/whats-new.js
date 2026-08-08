/**
 * What the panel can do, and when to say so.
 *
 * Separated from the element that draws it, the same way the folding toolbar and the
 * playback ladder are: the decision is the part worth being sure about, and node can be sure
 * about a pure function without a browser. `views/whats-new.js` is the dialog.
 *
 * The copy is about what a person gets, not about what changed in the code. A changelog
 * belongs in the release notes; this is the panel introducing itself.
 */

/**
 * Everything the panel can do, in the order somebody meets it.
 *
 * `beta` marks the ones that are off until switched on, because "why don't I have that" is
 * the question this is here to prevent, not cause.
 */
export const FEATURES = [
  {
    tone: "person",
    icon: "mdi:calendar-month-outline",
    title: "One timeline across every camera",
    text:
      "Every recorder's detections in a single list, with the clip one click away and the " +
      "playhead already at the event rather than at the top of a five-minute segment.",
    where: "Pick cameras and a day in the toolbar above.",
  },
  {
    tone: "vehicle",
    icon: "mdi:cloud-off-outline",
    title: "An off-site copy of what mattered",
    text:
      "A clip of each detection uploaded to your own OneDrive, event by event, so the " +
      "footage outlives the recorder it was written on. One per NVR, each with its own " +
      "quota and its own switch to automate.",
    where: "Settings → Devices & services → Reolink Stamina → <b>Add cloud sync</b>.",
  },
  {
    tone: "alert",
    icon: "mdi:circle-slice-8",
    title: "Learning what is normal",
    beta: true,
    text:
      "Marks the handful of events that are unusual for a camera — not by recognising " +
      "anything, but by counting. The cat that crosses at one in the morning every night " +
      "is ordinary; a person doing it is not. Nothing is recorded until you switch it on, " +
      "and it needs a week or so before it can say anything.",
    where: "Reolink Stamina → <b>Configure</b> → Learn what is normal.",
  },
  {
    tone: "motion",
    icon: "mdi:chart-box-outline",
    title: "See what a camera has learned",
    beta: true,
    text:
      "The chart button on any camera opens what it has actually counted: when it sees each " +
      "kind of thing, how long they last, what fired before them, and what your signals were " +
      "doing at the time. The toolbar has one for every camera at once, where which camera " +
      "becomes a distribution of its own. It is also how you spot a camera that has learned " +
      "something wrong.",
    where: "The chart button beside a camera, or in the toolbar.",
  },
  {
    tone: "animal",
    icon: "mdi:video-switch-outline",
    title: "Playback that works on more browsers",
    beta: true,
    text:
      "Recordings normally go straight from the recorder to your browser. When that draws " +
      "a black window — H.265 on Chrome or Firefox, or anything on an iPhone — this " +
      "converts them instead, and the player says which route the picture took.",
    where: "Reolink Stamina → <b>Configure</b> → Adaptive playback.",
  },
];

/**
 * Whether to introduce the panel, given what this browser last saw.
 *
 * @param {string|null|undefined} seen the version this browser was last shown
 * @param {string|null|undefined} current the version running now
 * @param {boolean} returning whether this browser has used the panel before
 */
export function shouldIntroduce(seen, current, returning) {
  // Nothing to remember against, so opening would mean opening on every single load.
  if (!current) return false;
  // A first install explains itself: somebody chose this five minutes ago and read something
  // about it to do so. A dialog repeating that is how people learn to dismiss dialogs unread.
  if (!returning) return false;
  // Deliberately not a version comparison — "different from what you were last shown" needs
  // no opinion about which way round two version strings go.
  return seen !== current;
}

// Small numbers read as words in a sentence; anything larger than this list will ever be
// falls back to digits rather than to invented vocabulary.
const WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight"];

function count(number) {
  return WORDS[number] || String(number);
}

/**
 * The line under the heading, counted from the list rather than written next to it.
 *
 * It used to say "three of these are here already" beside a list holding two, because the
 * list gained an entry and the sentence did not. A sentence that states a number about data
 * sitting a few lines below it should be derived from that data.
 */
export function summarise(features = FEATURES) {
  const beta = features.filter((feature) => feature.beta).length;
  const ready = features.length - beta;
  const opener =
    ready === 1
      ? "One of these is ready to use"
      : `${count(ready)[0].toUpperCase()}${count(ready).slice(1)} of these are ready to use`;
  const rest =
    beta === 0
      ? ""
      : beta === 1
        ? "; the one marked beta is off until you switch it on"
        : `; the ${count(beta)} marked beta are off until you switch them on`;
  return `${opener}${rest}. You can open this again from the header.`;
}
