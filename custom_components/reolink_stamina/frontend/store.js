/**
 * Panel state, and the orchestration that keeps the UI responsive.
 *
 * The rule the whole panel is built around: never block on the NVR. The backend answers
 * every subscription immediately from cache and pushes patches as searches finish, so
 * this store's job is to merge those patches into a stable view model and tell the
 * views what changed — and to make it obvious in the UI which parts are still catching
 * up rather than silently showing stale numbers.
 */

import { DEFAULT_FILTERS, FILTER_GROUPS, addDays, isoDate, parseIsoDate } from "./format.js";

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

    this.nvrs = [];
    this.options = {};
    this.searchWindowDays = 30;
    this.loadingNvrs = true;
    this.nvrError = null;

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

    /**
     * Detections from Home Assistant's recorder, keyed by `entry|channel`.
     *
     * Only ever an annotation on rows that already exist: the NVR says which triggers a
     * segment carried, this says how many times each one fired. Arrives after the list, so
     * counts appear when the recorder answers and are simply absent when it cannot.
     */
    this.detectionRuns = new Map();
    this._countsCache = new Map();

    this.truncated = false;
    this.selectedEventId = null;
    this.setupDone = false;

    this._eventsCache = null;
    this._eventsUnsub = null;
    this._calendarUnsub = null;
    this._generation = 0;
    this._resubscribeTimer = null;
  }

  // ------------------------------------------------------------------ lifecycle

  async init() {
    this._restore();
    await this.loadNvrs();
    // Only prune once the NVR list is known, so a temporarily unloaded NVR does not
    // silently wipe a saved selection.
    this._pruneSelection();
    if (this.cameras.length === 0) this._selectAllCameras();
    this.setupDone = this.setupDone && this.cameras.length > 0;
    this._emit();
    this._resubscribe();
  }

  async loadNvrs() {
    try {
      const result = await this.api.nvrs();
      this.nvrs = result.nvrs || [];
      this.options = result.options || {};
      this.searchWindowDays = result.search_window_days || 30;
      this.nvrError = null;
      // Opting out of the detections-only default brings back scheduled and
      // unlabelled footage. Applied only before the user has set their own filters.
      if (!this._restoredFilters && this.options.hide_timer === false) {
        this.filters.add("timer");
        this.filters.add("unclassified");
      }
    } catch (err) {
      this.nvrError = err?.message || String(err);
      this.nvrs = [];
    } finally {
      this.loadingNvrs = false;
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

  get usableNvrs() {
    return this.nvrs.filter((nvr) => nvr.status === "ok");
  }

  get unavailableNvrs() {
    return this.nvrs.filter((nvr) => nvr.status !== "ok");
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

  toggleNvr(entryId) {
    const nvr = this.nvrs.find((item) => item.entry_id === entryId);
    if (!nvr) return;
    const playable = nvr.cameras.filter((camera) => camera.can_playback);
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

  isNvrFullySelected(entryId) {
    const nvr = this.nvrs.find((item) => item.entry_id === entryId);
    if (!nvr) return false;
    const playable = nvr.cameras.filter((camera) => camera.can_playback);
    return (
      playable.length > 0 &&
      playable.every((camera) => this.isCameraSelected(entryId, camera.channel))
    );
  }

  isNvrPartiallySelected(entryId) {
    return (
      !this.isNvrFullySelected(entryId) &&
      this.cameras.some((camera) => camera.entry_id === entryId)
    );
  }

  _selectAllCameras() {
    this.cameras = this.usableNvrs.flatMap((nvr) =>
      nvr.cameras
        .filter((camera) => camera.can_playback)
        .map((camera) => ({ entry_id: nvr.entry_id, channel: camera.channel }))
    );
  }

  _pruneSelection() {
    const valid = new Set(
      this.usableNvrs.flatMap((nvr) =>
        nvr.cameras.map((camera) => this.cameraKey(nvr.entry_id, camera.channel))
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
      this._emit();
      return;
    }

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
      this.nvrError = err?.message || String(err);
      this._emit();
    });

    this._subscribeCalendar(targets, generation, force);
    this._loadDetectionCounts(targets, generation);
  }

  /**
   * Fetch the recorder's detections for the range currently shown.
   *
   * Fire and forget: the rows are already on screen from cache, and the counts are an
   * annotation that lands whenever the recorder gets round to it. A failure here means no
   * counts, never a broken list.
   */
  async _loadDetectionCounts(targets, generation) {
    try {
      const result = await this.api.detectionsRange({
        targets,
        startDate: this.startDate,
        endDate: this.endDate,
      });
      if (generation !== this._generation) return;
      // Parsed to epoch milliseconds on arrival. The recorder reports UTC while the NVR
      // reports the device's own offset, so comparing the two as strings silently counts
      // the wrong rows — and parsing per row per render would repeat that work thousands
      // of times over a 30-day range.
      this._setDetectionRuns(
        (result.cameras || []).map((camera) => [
          camera.key,
          (camera.detections || []).map((run) => ({ kind: run.kind, at: Date.parse(run.at) })),
        ])
      );
    } catch {
      if (generation !== this._generation) return;
      this._setDetectionRuns([]);
    }
  }

  _setDetectionRuns(entries) {
    this.detectionRuns = new Map(entries);
    // Counts are memoised per row and only these runs can change them: a row's window is
    // fixed by its id, so the cache outlives the patches that arrive for other rows.
    this._countsCache = new Map();
    this._emit();
  }

  /**
   * How many times each kind of detection fired inside one row, by `kind`.
   *
   * Counted by where a detection *started*, so a person walking across a segment boundary
   * is counted once, against the row the recorder tagged for it — the alternative
   * double-counts every event that spans a split.
   */
  detectionCounts(event) {
    const cached = this._countsCache.get(event.id);
    if (cached) return cached;

    const counts = new Map();
    const runs = this.detectionRuns.get(this.cameraKey(event.entry_id, event.channel));
    if (runs) {
      const from = Date.parse(event.start);
      const to = Date.parse(event.end);
      for (const run of runs) {
        if (run.at >= from && run.at < to) {
          counts.set(run.kind, (counts.get(run.kind) || 0) + 1);
        }
      }
    }
    this._countsCache.set(event.id, counts);
    return counts;
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
        const triggers = event.triggers || [];
        if (triggers.length === 0) {
          // Continuous recording, which real NVRs report with no trigger flag at all.
          // Filterable in its own right, because it dominates a 24/7 recorder.
          if (!showUnclassified) continue;
        } else if (!triggers.some((trigger) => enabled.has(trigger))) {
          continue;
        }
        all.push(event);
      }
    }
    all.sort((a, b) => (a.start < b.start ? 1 : a.start > b.start ? -1 : 0));
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
   * this is the overwhelming majority of what the NVR returned.
   */
  get unlabelledSkipped() {
    let total = 0;
    for (const bucket of this.buckets.values()) total += bucket.unlabelled_skipped || 0;
    return total;
  }

  /**
   * True when the range holds recordings the NVR gave no trigger for.
   *
   * A camera that records on events gets these kept rather than discarded — for it they
   * *are* the events — so they can turn up whatever the integration's unlabelled option
   * says, and the filter for them has to be offered whenever they do.
   */
  get hasUnclassifiedEvents() {
    for (const bucket of this.buckets.values()) {
      for (const event of bucket.events || []) {
        if ((event.triggers || []).length === 0) return true;
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
    const nvr = this.nvrs.find((item) => item.entry_id === entryId);
    const camera = nvr?.cameras.find((item) => item.channel === channel);
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
