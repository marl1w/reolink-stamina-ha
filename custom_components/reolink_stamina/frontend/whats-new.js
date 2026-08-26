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
 * `beta` no longer means "off until switched on" — there is nothing to switch on any more,
 * because a setup with six decisions in it is a setup most people get wrong. It means the
 * feature is newer than the rest and has met less hardware, which is worth saying plainly
 * to somebody deciding how much to trust it.
 */
export const FEATURES = [
  {
    tone: "person",
    icon: "mdi:calendar-month-outline",
    title: "One timeline across every camera",
    text:
      "Every recorder's detections in one list, with the clip one click away and the " +
      "playhead already at the event.",
    where: "Pick cameras and a day in the toolbar above.",
  },
  {
    tone: "vehicle",
    icon: "mdi:cloud-off-outline",
    title: "A second copy of what mattered",
    text:
      "A clip of each detection sent to your <b>Synology</b>, <b>WebDAV</b> or <b>SFTP</b> " +
      "NAS, or to <b>OneDrive</b> or <b>Google Drive</b>, so the footage outlives the " +
      "recorder. One per NVR, each with its own quota and switch.",
    where: "Settings → Devices & services → Reolink Stamina → <b>Add cloud sync</b>.",
  },
  {
    tone: "alert",
    icon: "mdi:circle-slice-8",
    title: "Learning what is normal",
    beta: true,
    text:
      "Marks the few events that are unusual for a camera — by counting, not by " +
      "recognising anything. Stays on this machine, and needs a week or so before it can " +
      "say anything.",
    where: "Marked rows appear in the timeline. Choose what else it counts in <b>Configure</b>.",
  },
  {
    tone: "motion",
    icon: "mdi:chart-box-outline",
    title: "See what a camera has learned",
    beta: true,
    text:
      "What a camera has actually counted: when it sees each kind of thing, how long they " +
      "last, what fired before. Also how you spot one that has learned something wrong.",
    where: "The chart button beside a camera, or in the toolbar.",
  },
  {
    tone: "animal",
    icon: "mdi:video-switch-outline",
    title: "Playback that works on more browsers",
    beta: true,
    text:
      "When direct play draws a black window — H.265 on Chrome or Firefox, anything on an " +
      "iPhone — this converts instead. <b>Home Hubs</b> now play and save natively.",
    where: "Nothing to switch on: the player tries it only when direct play fails.",
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
  const rest =
    beta === 0
      ? ""
      : beta === 1
        ? "; the one marked beta is newer and has met less hardware"
        : `; the ${count(beta)} marked beta are newer and have met less hardware`;
  return `All of these are on, with nothing to switch on${rest}. You can open this again from the header.`;
}
