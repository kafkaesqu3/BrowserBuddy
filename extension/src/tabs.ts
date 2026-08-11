import type { StorageArea, TabRecord } from "./types";

/** The subset of browser.tabs.Tab we consume (keeps tests browser-free). */
export interface BrowserTab {
  id?: number;
  windowId?: number;
  url?: string;
  title?: string;
  groupId?: number;
  cookieStoreId?: string;
}

interface PerTab {
  opened_at: number;
  last_activated_at: number | null;
  active_ms: number;
}

interface TrackerState {
  activeTabId: number | null;
  activeSince: number | null;
  perTab: Record<string, PerTab>;
}

const STATE_KEY = "bb:tabstate";
const EMPTY: TrackerState = { activeTabId: null, activeSince: null, perTab: {} };

/** Active-time accounting. State lives in storage.session so it survives
 * background-worker suspension but resets with the browser (like tabs do). */
export class TabTracker {
  // Same non-transactional-storage hazard as EventQueue: interleaved listeners
  // must not race the read-modify-write.
  private mutex: Promise<unknown> = Promise.resolve();

  constructor(
    private session: StorageArea,
    private now: () => number,
  ) {}

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.mutex.then(fn, fn);
    this.mutex = next.catch(() => undefined);
    return next;
  }

  private async load(): Promise<TrackerState> {
    const stored = (await this.session.get(STATE_KEY))[STATE_KEY] as TrackerState | undefined;
    return stored ?? structuredClone(EMPTY);
  }

  private async store(state: TrackerState): Promise<void> {
    await this.session.set({ [STATE_KEY]: state });
  }

  private ensure(state: TrackerState, tabId: number): PerTab {
    const key = String(tabId);
    state.perTab[key] ??= { opened_at: this.now(), last_activated_at: null, active_ms: 0 };
    return state.perTab[key];
  }

  /** Bank active time for the previously active tab. */
  private bank(state: TrackerState): void {
    if (state.activeTabId === null || state.activeSince === null) return;
    // Clamp: a wall-clock regression must not produce negative active_ms —
    // the backend's ge=0 validation would reject every subsequent tab_state
    // containing this tab.
    this.ensure(state, state.activeTabId).active_ms += Math.max(0, this.now() - state.activeSince);
    state.activeTabId = null;
    state.activeSince = null;
  }

  /** Precondition (enforced by the background wiring, not here): call only for
   * the focused window's active tab — tabs.onActivated fires for background
   * windows too, and recordFocusLost must run when window focus leaves the
   * browser. */
  recordActivation(tabId: number, _windowId: number): Promise<void> {
    return this.serialize(() => this.recordActivationImpl(tabId));
  }

  private async recordActivationImpl(tabId: number): Promise<void> {
    const state = await this.load();
    this.bank(state);
    const entry = this.ensure(state, tabId);
    const t = this.now();
    entry.last_activated_at = t;
    state.activeTabId = tabId;
    state.activeSince = t;
    await this.store(state);
  }

  recordFocusLost(): Promise<void> {
    return this.serialize(() => this.recordFocusLostImpl());
  }

  private async recordFocusLostImpl(): Promise<void> {
    const state = await this.load();
    this.bank(state);
    await this.store(state);
  }

  /** Build TabRecords for all live tabs; prune accounting for closed ones. */
  snapshot(
    tabs: BrowserTab[],
    containerName: (cookieStoreId?: string) => Promise<string | null>,
  ): Promise<TabRecord[]> {
    return this.serialize(() => this.snapshotImpl(tabs, containerName));
  }

  private async snapshotImpl(
    tabs: BrowserTab[],
    containerName: (cookieStoreId?: string) => Promise<string | null>,
  ): Promise<TabRecord[]> {
    const state = await this.load();
    const records: TabRecord[] = [];
    const liveKeys = new Set<string>();
    for (const tab of tabs) {
      if (tab.id === undefined || tab.windowId === undefined || !tab.url) continue;
      liveKeys.add(String(tab.id));
      const entry = this.ensure(state, tab.id);
      let activeMs = entry.active_ms;
      if (state.activeTabId === tab.id && state.activeSince !== null) {
        activeMs += Math.max(0, this.now() - state.activeSince); // include the open interval
      }
      records.push({
        tab_id: tab.id,
        window_id: tab.windowId,
        url: tab.url,
        title: tab.title ?? "",
        group_id: tab.groupId === undefined || tab.groupId === -1 ? null : tab.groupId,
        container: await containerName(tab.cookieStoreId),
        opened_at: entry.opened_at,
        last_activated_at: entry.last_activated_at,
        active_ms: activeMs,
      });
    }
    for (const key of Object.keys(state.perTab)) {
      if (!liveKeys.has(key)) delete state.perTab[key];
    }
    if (state.activeTabId !== null && !liveKeys.has(String(state.activeTabId))) {
      state.activeTabId = null;
      state.activeSince = null;
    }
    await this.store(state);
    return records;
  }

  /** Current banked active time for a tab (includes the live open interval
   * if it's the currently active tab), without mutating stored state. Used
   * by dwell accounting to snapshot/diff a tab's clock at page boundaries
   * (see background.ts rebaseDwell/flushDwell). Returns 0 for a tab this
   * tracker has never seen. */
  activeMs(tabId: number): Promise<number> {
    return this.serialize(() => this.activeMsImpl(tabId));
  }

  private async activeMsImpl(tabId: number): Promise<number> {
    const state = await this.load();
    const entry = state.perTab[String(tabId)];
    if (!entry) return 0;
    let ms = entry.active_ms;
    if (state.activeTabId === tabId && state.activeSince !== null) {
      ms += Math.max(0, this.now() - state.activeSince);
    }
    return ms;
  }
}
