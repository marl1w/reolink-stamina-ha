/** Formatting helpers and the trigger vocabulary shared across views. */

/**
 * How each Reolink trigger is presented. Order matters: the most meaningful trigger for
 * a recording is shown first, so a clip that is both "motion" and "person" reads as a
 * person. Names match reolink_aio's VOD_trigger members, lowercased.
 */
export const TRIGGERS = {
  person: { label: "Person", icon: "mdi:walk", tone: "person", rank: 0 },
  vehicle: { label: "Vehicle", icon: "mdi:car", tone: "vehicle", rank: 1 },
  animal: { label: "Animal", icon: "mdi:paw", tone: "animal", rank: 2 },
  face: { label: "Face", icon: "mdi:face-recognition", tone: "person", rank: 3 },
  doorbell: { label: "Doorbell", icon: "mdi:doorbell-video", tone: "alert", rank: 4 },
  package: { label: "Package", icon: "mdi:package-variant-closed", tone: "alert", rank: 5 },
  crying: { label: "Crying", icon: "mdi:emoticon-cry-outline", tone: "alert", rank: 6 },
  crossline: { label: "Line crossed", icon: "mdi:vector-line", tone: "alert", rank: 7 },
  intrusion: { label: "Intrusion", icon: "mdi:shield-alert-outline", tone: "alert", rank: 8 },
  linger: { label: "Loitering", icon: "mdi:account-clock-outline", tone: "alert", rank: 9 },
  forgotten_item: { label: "Item left", icon: "mdi:bag-personal-outline", tone: "alert", rank: 10 },
  taken_item: { label: "Item taken", icon: "mdi:bag-personal-off-outline", tone: "alert", rank: 11 },
  io: { label: "Sensor", icon: "mdi:electric-switch", tone: "neutral", rank: 12 },
  motion: { label: "Motion", icon: "mdi:motion-sensor", tone: "motion", rank: 13 },
  timer: { label: "Scheduled", icon: "mdi:clock-outline", tone: "neutral", rank: 14 },
};

/** The filters offered in the toolbar, in display order. */
export const FILTER_GROUPS = [
  { id: "person", label: "Person", icon: "mdi:walk", triggers: ["person", "face"] },
  { id: "vehicle", label: "Vehicle", icon: "mdi:car", triggers: ["vehicle"] },
  { id: "animal", label: "Animal", icon: "mdi:paw", triggers: ["animal"] },
  {
    id: "other",
    label: "Other alerts",
    icon: "mdi:bell-outline",
    triggers: [
      "doorbell",
      "package",
      "face",
      "crying",
      "crossline",
      "intrusion",
      "linger",
      "forgotten_item",
      "taken_item",
      "io",
    ],
  },
  { id: "motion", label: "Motion", icon: "mdi:motion-sensor", triggers: ["motion"] },
  { id: "timer", label: "Scheduled", icon: "mdi:clock-outline", triggers: ["timer"] },
  // Continuous recording often carries no trigger flag at all — real devices report
  // VOD_trigger.NONE rather than TIMER for it. Without its own group these rows could
  // not be filtered, and they outnumber real events by roughly 30:1 on a 24/7 recorder.
  //
  // Labelled to match the chip those rows carry in the list: whatever the recorder meant
  // by them, what the user sees is "Recording", so that is what the filter says too.
  {
    id: "unclassified",
    label: "Recording",
    icon: "mdi:video-outline",
    triggers: [],
    matchesUnclassified: true,
  },
];

/**
 * Filters enabled the first time the panel is opened.
 *
 * Detections only. On a recorder with 24/7 recording enabled, scheduled and unlabelled
 * footage outnumbers real detections by roughly 30:1, and motion fires constantly — so
 * both are available as filters but off by default. Person, vehicle and animal are what
 * the panel is for.
 */
export const DEFAULT_FILTERS = ["person", "vehicle", "animal", "other"];

export function triggerMeta(name) {
  return (
    TRIGGERS[name] || {
      label: name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
      icon: "mdi:help-circle-outline",
      tone: "neutral",
      rank: 99,
    }
  );
}

/** Sort an event's triggers so the most significant reads first. */
export function sortTriggers(triggers) {
  return [...triggers].sort((a, b) => triggerMeta(a).rank - triggerMeta(b).rank);
}

/** The single trigger that best characterises an event, for the row's leading icon. */
export function primaryTrigger(triggers) {
  if (!triggers || triggers.length === 0) return null;
  return sortTriggers(triggers)[0];
}

export function formatTime(iso, locale) {
  const date = new Date(iso);
  return date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${pad(minutes)}:${pad(secs)}`;
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatSize(bytes) {
  if (!bytes) return "—";
  const units = ["B", "kB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** "just now" / "3 min ago", for cache freshness. */
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function isoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function parseIsoDate(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

export function addDays(date, days) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

/** "Today" / "Yesterday" / "Monday 3 August", for day headings. */
export function formatDayLabel(isoValue, locale) {
  const date = parseIsoDate(isoValue);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((date - today) / 86400000);
  if (diff === 0) return "Today";
  if (diff === -1) return "Yesterday";
  return date.toLocaleDateString(locale, {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
}

export function streamLabel(stream) {
  switch (stream) {
    case "main":
      return "High";
    case "sub":
      return "Low";
    case "autotrack_main":
      return "Telephoto high";
    case "autotrack_sub":
      return "Telephoto low";
    default:
      return stream;
  }
}
