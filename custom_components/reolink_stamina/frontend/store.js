/**
 * Panel state, and the orchestration that keeps the UI responsive.
 *
 * The rule the whole panel is built around: never block on the device. The backend answers
 * every subscription immediately from cache and pushes patches as searches finish, so
 * this store's job is to merge those patches into a stable view model and tell the
 * views what changed — and to make it obvious in the UI which parts are still catching
 * up rather than silently showing stale numbers.
 */

import {
  DEFAULT_FILTERS,
  FILTER_GROUPS,
  addDays,
  isoDate,
  parseIsoDate,
  sortTriggers,
} from "./format.js";

const STORAGE_KEY = "reolink_stamina.ui";

/** Presets offered in the toolbar. */
export const RANGE_PRESETS = [
  { id: "today", label: "Today", days: 0 },
  { id: "yesterday", label: "Yesterday", days: 1, single: true },
  { id: "7d", label: "Last 7 days", days: 6 },
  { id: "30d", label: "Last 30 days", days: 29 },
];

export class StaminaStore extends EventTarget {
  constructor(api) {
    super();
    this.api = api;

    this.devices = [];
    this.options = {};
    this.searchWindowDays = 30;
    this.loadingDevices = true;
    this.deviceError = null;

    /** @type {{entry_id: string, channel: number}[]} */
    this.cameras = [];
    this.startDate = isoDate(new Date());
    this.endDate = isoDate(new Date());
    this.rangePreset = "today";

    /** Enabled filter group ids. Detections only until the user says otherwise. */
    this.filters = new Set(DEFAULT_FILTERS);

    /** @type {Map<string, object>} keyed by `entry|channel|date` */
    this.buckets = new Map();
    /** @type {Map<string, number[]>} keyed by `entry|channel` */
    this.calendar = new Map();
    this.calendarMonth = null;

    this.truncated = false;
    this.selectedEventId = null;
    this.setupDone = false;

    /** The release this browser was last shown the introduction for. */
    this.seenVersion = null;
    /** Whether this browser has used the panel before, which a fresh install has not. */
    this.returning = false;

    this._eventsCache = null;
    this._eventsUnsub = null;
    this._calendarUnsub = null;
    this._generation = 0;
    this._resubscribeTimer = null;

    /**
     * What the "learn what is normal" beta knows, keyed by `entry_id:channel`.
     *
     * `{ state, coverage, detections: [...] }`, where each detection is one thing the
     * camera saw rather than one row: a five-minute segment can hold several, which is why
     * a row looks its own window up rather than matching a timestamp.
     *
     * Fetched rather than subscribed. It changes when the model is rebuilt — once a night —
     * so a subscription would cost a socket to deliver nothing almost all of the time.
     */
    this.relevance = new Map();
    this.relevanceEnabled = false;
    /** Show only the rows the model found unusual. Client-side, like every other filter. */
    this.unusualOnly = false;
    this._relevanceKey = null;
  }

  // ------------------------------------------------------------------ lifecycle

  async init() {
    this._restore();
    await this.loadDevices();
    // Only prune once the device list is known, so a temporarily unloaded one does not
    // silently wipe a saved selection.
    this._pruneSelection();
    if (this.cameras.length === 0) this._selectAllCameras();
    this.setupDone = this.setupDone && this.cameras.length > 0;
    this._emit();
    this._resubscribe();
  }

  async loadDevices() {
    try {
      const result = await this.api.devices();
      this.devices = result.devices || [];
      this.options = result.options || {};
      this.searchWindowDays = result.search_window_days || 30;
      this.deviceError = null;
      // Opting out of the detections-only default brings back scheduled and
      // unlabelled footage. Applied only before the user has set their own filters.
      if (!this._restoredFilters && this.options.hide_timer === false) {
        this.filters.add("timer");
        this.filters.add("unclassified");
      }
    } catch (err) {
      this.deviceError = err?.message || String(err);
      this.devices = [];
    } finally {
      this.loadingDevices = false;
    }
    this._emit();
  }

  destroy() {
    this._generation += 1;
    if (this._resubscribeTimer) clearTimeout(this._resubscribeTimer);
    this._unsubscribe();
  }

  _unsubscribe() {
    const events = this._eventsUnsub;
    const calendar = this._calendarUnsub;
    this._eventsUnsub = null;
    this._calendarUnsub = null;
    // Unsubscribes resolve asynchronously; failures are harmless here.
    Promise.resolve(events).then((fn) => fn && fn()).catch(() => {});
    Promise.resolve(calendar).then((fn) => fn && fn()).catch(() => {});
  }

  // ------------------------------------------------------------------ selection

  get usableDevices() {
    return this.devices.filter((device) => device.status === "ok");
  }

  get unavailableDevices() {
    return this.devices.filter((device) => device.status !== "ok");
  }

  cameraKey(entryId, channel) {
    return `${entryId}|${channel}`;
  }

  isCameraSelected(entryId, channel) {
    return this.cameras.some(
      (camera) => camera.entry_id === entryId && camera.channel === channel
    );
  }

  toggleCamera(entryId, channel) {
    if (this.isCameraSelected(entryId, channel)) {
      this.cameras = this.cameras.filter(
        (camera) => !(camera.entry_id === entryId && camera.channel === channel)
      );
    } else {
      this.cameras = [...this.cameras, { entry_id: entryId, channel }];
    }
    this._afterSelectionChange();
  }

  toggleDevice(entryId) {
    const device = this.devices.find((item) => item.entry_id === entryId);
    if (!device) return;
    const playable = device.cameras.filter((camera) => camera.can_playback);
    const allSelected = playable.every((camera) =>
      this.isCameraSelected(entryId, camera.channel)
    );
    this.cameras = this.cameras.filter((camera) => camera.entry_id !== entryId);
    if (!allSelected) {
      this.cameras = [
        ...this.cameras,
        ...playable.map((camera) => ({ entry_id: entryId, channel: camera.channel })),
      ];
    }
    this._afterSelectionChange();
  }

  isDeviceFullySelected(entryId) {
    const device = this.devices.find((item) => item.entry_id === entryId);
    if (!device) return false;
    const playable = device.cameras.filter((camera) => camera.can_playback);
    return (
      playable.length > 0 &&
      playable.every((camera) => this.isCameraSelected(entryId, camera.channel))
    );
  }

  isDevicePartiallySelected(entryId) {
    return (
      !this.isDeviceFullySelected(entryId) &&
      this.cameras.some((camera) => camera.entry_id === entryId)
    );
  }

  _selectAllCameras() {
    this.cameras = this.usableDevices.flatMap((device) =>
      device.cameras
        .filter((camera) => camera.can_playback)
        .map((camera) => ({ entry_id: device.entry_id, channel: camera.channel }))
    );
  }

  _pruneSelection() {
    const valid = new Set(
      this.usableDevices.flatMap((device) =>
        device.cameras.map((camera) => this.cameraKey(device.entry_id, camera.channel))
      )
    );
    this.cameras = this.cameras.filter((camera) =>
      valid.has(this.cameraKey(camera.entry_id, camera.channel))
    );
  }

  _afterSelectionChange() {
    this._persist();
    this._emit();
    this._scheduleResubscribe();
  }

  completeSetup() {
    this.setupDone = true;
    this._persist();
    this._emit();
    this._resubscribe();
  }

  reopenSetup() {
    this.setupDone = false;
    this._persist();
    this._emit();
  }

  // ----------------------------------------------------------------- date range

  setRangePreset(id) {
    const preset = RANGE_PRESETS.find((item) => item.id === id);
    if (!preset) return;
    const today = new Date();
    if (preset.single) {
      const day = isoDate(addDays(today, -preset.days));
      this.startDate = day;
      this.endDate = day;
    } else {
      this.endDate = isoDate(today);
      this.startDate = isoDate(addDays(today, -preset.days));
    }
    this.rangePreset = id;
    this._afterRangeChange();
  }

  setSingleDay(isoValue) {
    this.startDate = isoValue;
    this.endDate = isoValue;
    this.rangePreset = null;
    this._afterRangeChange();
  }

  /** Step the range by whole days, keeping its length. */
  shiftDays(delta) {
    const start = addDays(parseIsoDate(this.startDate), delta);
    const end = addDays(parseIsoDate(this.endDate), delta);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    if (end > today) return false;
    const earliest = addDays(today, -this.searchWindowDays);
    if (start < earliest) return false;
    this.startDate = isoDate(start);
    this.endDate = isoDate(end);
    this.rangePreset = null;
    this._afterRangeChange();
    return true;
  }

  get canShiftForward() {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return parseIsoDate(this.endDate) < today;
  }

  get canShiftBack() {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return parseIsoDate(this.startDate) > addDays(today, -this.searchWindowDays);
  }

  get isSingleDay() {
    return this.startDate === this.endDate;
  }

  _afterRangeChange() {
    // Drop rows for days no longer in range so the list cannot show stale days while
    // the new snapshot is still arriving.
    this.buckets.clear();
    this.selectedEventId = null;
    this._persist();
    this._emit();
    this._scheduleResubscribe();
  }

  // -------------------------------------------------------------------- filters

  toggleFilter(id) {
    if (this.filters.has(id)) this.filters.delete(id);
    else this.filters.add(id);
    this._persist();
    // Filtering is client-side, so this is instant — no refetch.
    this._emit();
  }

  setAllFilters(enabled) {
    this.filters = enabled ? new Set(FILTER_GROUPS.map((group) => group.id)) : new Set();
    this._persist();
    this._emit();
  }

  get enabledTriggers() {
    const triggers = new Set();
    for (const group of FILTER_GROUPS) {
      if (!this.filters.has(group.id)) continue;
      for (const trigger of group.triggers) triggers.add(trigger);
    }
    return triggers;
  }

  // --------------------------------------------------------------- subscriptions

  _scheduleResubscribe() {
    // Coalesce rapid selection changes into a single subscription.
    if (this._resubscribeTimer) clearTimeout(this._resubscribeTimer);
    this._resubscribeTimer = setTimeout(() => {
      this._resubscribeTimer = null;
      this._resubscribe();
    }, 180);
  }

  async _resubscribe({ force = false } = {}) {
    this._generation += 1;
    const generation = this._generation;
    this._unsubscribe();

    if (this.cameras.length === 0) {
      this.buckets.clear();
      this.relevance = new Map();
      this._relevanceKey = null;
      this._emit();
      return;
    }

    // Alongside the subscription rather than inside it: the rows must not wait on this, and
    // this must not be refetched every time a patch arrives.
    this.refreshRelevance();

    const targets = this.cameras.map((camera) => ({
      entry_id: camera.entry_id,
      channel: camera.channel,
    }));

    const eventsPromise = this.api.subscribeEvents(
      { targets, startDate: this.startDate, endDate: this.endDate, force },
      (message) => {
        if (generation !== this._generation) return;
        this._onEventsMessage(message);
      }
    );
    this._eventsUnsub = eventsPromise;
    eventsPromise.catch((err) => {
      if (generation !== this._generation) return;
      this.deviceError = err?.message || String(err);
      this._emit();
    });

    this._subscribeCalendar(targets, generation, force);
  }

  /**
   * What a row is, as decided when the day was searched: the detection sensors' verdict
   * where there is one, the recorder's own flags where there is not.
   *
   * Merged in the backend rather than here, so the same answer drives the rows, the
   * filters and what is worth keeping in the cache, and so it survives in storage instead
   * of being recomputed per render.
   */
  eventKinds(event) {
    return sortTriggers(event.kinds || event.triggers || []);
  }

  /** How many times each kind fired inside a row, by `kind`. */
  detectionCounts(event) {
    return new Map(Object.entries(event.counts || {}));
  }

  // ------------------------------------------------------- learning what is normal

  /** The camera key the journal uses, which is not the one the buckets use. */
  relevanceKey(entryId, channel) {
    return `${entryId}:${channel}`;
  }

  /** What is known about one camera: `collecting`, `too_few_events` or `active`. */
  relevanceFor(entryId, channel) {
    return this.relevance.get(this.relevanceKey(entryId, channel)) || null;
  }

  /**
   * What the model saw inside one row, most unusual first.
   *
   * A row is a stretch of recording and a detection is a moment inside it, so this is a
   * window lookup rather than a match: a five-minute segment can hold three detections, and
   * the row should reflect the most remarkable of them while the popover lists them all.
   */
  eventRelevance(event) {
    const known = this.relevanceFor(event.entry_id, event.channel);
    if (!known) return [];
    const start = Date.parse(event.start);
    const end = Date.parse(event.end || event.start);
    if (Number.isNaN(start)) return [];
    return known.detections
      .filter((item) => {
        const at = Date.parse(item.at);
        return at >= start && at <= (Number.isNaN(end) ? start : end);
      })
      .sort((a, b) => b.score - a.score);
  }

  /** Whether anything inside this row was found unusual. */
  isUnusual(event) {
    return this.eventRelevance(event).some((item) => item.unusual);
  }

  /** Record that the introduction has been seen for this release. */
  markIntroduced(version) {
    this.seenVersion = version || null;
    this.returning = true;
    this._persist();
  }

  toggleUnusualOnly() {
    this.unusualOnly = !this.unusualOnly;
    this._persist();
    this._emit();
  }

  /**
   * Ask each selected camera what it makes of the visible range.
   *
   * Deliberately not part of the event subscription. That one is a live thing patched as a
   * slow recorder answers; this changes when the model is rebuilt, which is once a night —
   * so it is fetched when the selection or the range settles, and never again in between.
   */
  async refreshRelevance() {
    const key = `${this.startDate}|${this.endDate}|${this.cameras
      .map((camera) => this.relevanceKey(camera.entry_id, camera.channel))
      .sort()
      .join(",")}`;
    if (key === this._relevanceKey) return;
    this._relevanceKey = key;

    if (this.cameras.length === 0) {
      this.relevance = new Map();
      return;
    }

    // The range is whole local days, and the journal is addressed in absolute time.
    const start = new Date(`${this.startDate}T00:00:00`);
    const end = new Date(`${this.endDate}T00:00:00`);
    end.setDate(end.getDate() + 1);

    const found = new Map();
    let enabled = false;
    await Promise.all(
      this.cameras.map(async (camera) => {
        try {
          const answer = await this.api.relevance({
            entryId: camera.entry_id,
            channel: camera.channel,
            start: start.toISOString(),
            end: end.toISOString(),
          });
          if (!answer || !answer.enabled) return;
          enabled = true;
          found.set(this.relevanceKey(camera.entry_id, camera.channel), {
            state: answer.state,
            coverage: answer.coverage || { days: 0, events: 0 },
            detections: answer.events || [],
          });
        } catch (err) {
          // A camera that cannot answer is not a reason to lose the ones that can, and this
          // is an ornament on a list that works perfectly well without it.
          console.debug("Relevance unavailable for a camera", err);
        }
      })
    );

    this.relevance = found;
    this.relevanceEnabled = enabled;
    if (!enabled) this.unusualOnly = false;
    this._emit();
  }

  _subscribeCalendar(targets, generation, force) {
    const anchor = parseIsoDate(this.endDate);
    const year = anchor.getFullYear();
    const month = anchor.getMonth() + 1;
    this.calendarMonth = `${year}-${month}`;

    const promise = this.api.subscribeCalendar(
      { targets, year, month, force },
      (message) => {
        if (generation !== this._generation) return;
        this._onCalendarMessage(message);
      }
    );
    this._calendarUnsub = promise;
    promise.catch(() => {
      // A missing calendar only costs the date picker its highlighting.
    });
  }

  /** Force a refresh of everything currently on screen. */
  refresh() {
    this._resubscribe({ force: true });
  }

  _onEventsMessage(message) {
    if (message.type === "snapshot") {
      this.buckets = new Map();
      for (const bucket of message.buckets || []) {
        this.buckets.set(bucket.key, bucket);
      }
      this.truncated = Boolean(message.truncated);
      this.primaryStream = message.primary_stream;
      this.secondaryStream = message.secondary_stream;
    } else if (message.type === "patch" && message.bucket) {
      this.buckets.set(message.bucket.key, message.bucket);
    } else {
      return;
    }
    this._emit();
  }

  _onCalendarMessage(message) {
    if (message.type === "snapshot") {
      this.calendar = new Map();
      for (const camera of message.cameras || []) {
        this.calendar.set(camera.key, camera.days || []);
      }
    } else if (message.type === "patch" && message.camera) {
      this.calendar.set(message.camera.key, message.camera.days || []);
    } else {
      return;
    }
    this._emit();
  }

  // ---------------------------------------------------------------- derived data

  /**
   * Every event in range, filtered and newest first.
   *
   * Memoised: several views read this per render, and a 30-day multi-camera range can hold
   * thousands of rows. The cache is dropped whenever state changes, in `_emit`.
   */
  get events() {
    if (this._eventsCache) return this._eventsCache;
    this._eventsCache = this._computeEvents();
    return this._eventsCache;
  }

  _computeEvents() {
    const enabled = this.enabledTriggers;
    const showUnclassified = this.filters.has("unclassified");
    const all = [];
    for (const bucket of this.buckets.values()) {
      for (const event of bucket.events || []) {
        const kinds = this.eventKinds(event);
        if (kinds.length === 0) {
          // Nothing detected and nothing tagged: continuous recording, which real devices
          // report with no trigger flag at all. Filterable in its own right, because it
          // dominates a 24/7 recorder.
          if (!showUnclassified) continue;
        } else if (!kinds.some((kind) => enabled.has(kind))) {
          continue;
        }
        all.push(event);
      }
    }
    all.sort((a, b) => (a.start < b.start ? 1 : a.start > b.start ? -1 : 0));
    // Last, so it narrows what the other filters left rather than competing with them:
    // "unusual" is a judgement about a row, not another kind of thing to show.
    if (this.unusualOnly && this.relevanceEnabled) {
      return all.filter((event) => this.isUnusual(event));
    }
    return all;
  }

  /** Events grouped by local day, for the list's sticky headings. */
  get eventsByDay() {
    const groups = new Map();
    for (const event of this.events) {
      const day = event.start.slice(0, 10);
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(event);
    }
    return [...groups.entries()].map(([day, events]) => ({ day, events }));
  }

  /**
   * Continuous recordings the backend discarded before sending anything.
   *
   * Reported so a short list is explained rather than mysterious: on a 24/7 recorder
   * this is the overwhelming majority of what the device returned.
   */
  get unlabelledSkipped() {
    let total = 0;
    for (const bucket of this.buckets.values()) total += bucket.unlabelled_skipped || 0;
    return total;
  }

  /**
   * True when the range holds recordings the device gave no trigger for.
   *
   * A camera that records on events gets these kept rather than discarded — for it they
   * *are* the events — so they can turn up whatever the integration's unlabelled option
   * says, and the filter for them has to be offered whenever they do.
   */
  get hasUnclassifiedEvents() {
    for (const bucket of this.buckets.values()) {
      for (const event of bucket.events || []) {
        // Same rule the list filters by, or the chip offering to show unclassified rows
        // would appear for rows the sensors have already classified.
        if (this.eventKinds(event).length === 0) return true;
      }
    }
    return false;
  }

  /** Total before filtering, so the UI can say "12 of 40 hidden by filters". */
  get totalEventCount() {
    let total = 0;
    for (const bucket of this.buckets.values()) total += (bucket.events || []).length;
    return total;
  }

  /** True while any camera-day in view is still being searched. */
  get isUpdating() {
    for (const bucket of this.buckets.values()) {
      if (bucket.updating) return true;
    }
    return false;
  }

  /** True before any camera-day in view has ever been loaded. */
  get isFirstLoad() {
    if (this.buckets.size === 0) return this.cameras.length > 0;
    for (const bucket of this.buckets.values()) {
      if (bucket.loaded) return false;
    }
    return true;
  }

  /** Still resolving which resolutions each clip exists in. */
  get availabilityPending() {
    for (const bucket of this.buckets.values()) {
      if (bucket.availability_pending) return true;
    }
    return false;
  }

  /** Age of the oldest data on screen, in seconds. */
  get oldestAge() {
    let oldest = null;
    for (const bucket of this.buckets.values()) {
      if (bucket.age === null || bucket.age === undefined) continue;
      if (oldest === null || bucket.age > oldest) oldest = bucket.age;
    }
    return oldest;
  }

  /** Distinct search errors currently affecting the view. */
  get errors() {
    const messages = new Set();
    for (const bucket of this.buckets.values()) {
      if (bucket.error) messages.add(bucket.error);
    }
    return [...messages];
  }

  /** Days of the shown month that contain recordings, across selected cameras. */
  get daysWithRecordings() {
    const days = new Set();
    for (const [key, list] of this.calendar) {
      const [entryId, channel] = key.split("|");
      if (!this.isCameraSelected(entryId, Number(channel))) continue;
      for (const day of list) days.add(day);
    }
    return days;
  }

  cameraLabel(entryId, channel) {
    const device = this.devices.find((item) => item.entry_id === entryId);
    const camera = device?.cameras.find((item) => item.channel === channel);
    return camera?.name || `Channel ${channel}`;
  }

  get selectedEvent() {
    if (!this.selectedEventId) return null;
    return this.events.find((event) => event.id === this.selectedEventId) || null;
  }

  selectEvent(id) {
    this.selectedEventId = id;
    this._emit();
  }

  /** Step to the adjacent event in the filtered list, for the player's prev/next. */
  stepEvent(delta) {
    const events = this.events;
    const index = events.findIndex((event) => event.id === this.selectedEventId);
    if (index === -1) return false;
    const next = events[index + delta];
    if (!next) return false;
    this.selectedEventId = next.id;
    this._emit();
    return true;
  }

  hasAdjacentEvent(delta) {
    const events = this.events;
    const index = events.findIndex((event) => event.id === this.selectedEventId);
    return index !== -1 && Boolean(events[index + delta]);
  }

  // ------------------------------------------------------------------ persistence

  _emit() {
    // Any state change invalidates the derived, filtered event list.
    this._eventsCache = null;
    this.dispatchEvent(new CustomEvent("changed"));
  }

  _persist() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          cameras: this.cameras,
          startDate: this.startDate,
          endDate: this.endDate,
          rangePreset: this.rangePreset,
          filters: [...this.filters],
          setupDone: this.setupDone,
          seenVersion: this.seenVersion,
        })
      );
    } catch {
      // Private browsing or a full quota: preferences simply do not persist.
    }
  }

  _restore() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    } catch {
      saved = null;
    }
    if (!saved) return;
    // Something was saved, so this browser has been here before — which is what tells a
    // first install apart from an upgrade.
    this.returning = true;
    if (typeof saved.seenVersion === "string") this.seenVersion = saved.seenVersion;

    if (Array.isArray(saved.cameras)) this.cameras = saved.cameras;
    if (Array.isArray(saved.filters)) {
      this.filters = new Set(saved.filters);
      this._restoredFilters = true;
    }
    this.setupDone = Boolean(saved.setupDone);

    // Restore the range only if it is still searchable; otherwise fall back to today.
    if (saved.rangePreset) {
      this.setRangePresetSilently(saved.rangePreset);
    } else if (saved.startDate && saved.endDate) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const earliest = addDays(today, -this.searchWindowDays);
      const start = parseIsoDate(saved.startDate);
      if (start >= earliest && parseIsoDate(saved.endDate) <= today) {
        this.startDate = saved.startDate;
        this.endDate = saved.endDate;
        this.rangePreset = null;
      }
    }
  }

  /** Apply a preset without triggering a resubscribe; used while restoring. */
  setRangePresetSilently(id) {
    const preset = RANGE_PRESETS.find((item) => item.id === id);
    if (!preset) return;
    const today = new Date();
    if (preset.single) {
      const day = isoDate(addDays(today, -preset.days));
      this.startDate = day;
      this.endDate = day;
    } else {
      this.endDate = isoDate(today);
      this.startDate = isoDate(addDays(today, -preset.days));
    }
    this.rangePreset = id;
  }
}
