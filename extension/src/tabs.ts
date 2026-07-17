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
  constructor(
    private session: StorageArea,
    private now: () => number,
  ) {}

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
    this.ensure(state, state.activeTabId).active_ms += this.now() - state.activeSince;
    state.activeTabId = null;
    state.activeSince = null;
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- windowId reserved for future window-scoped accounting
  async recordActivation(tabId: number, windowId: number): Promise<void> {
    const state = await this.load();
    this.bank(state);
    const entry = this.ensure(state, tabId);
    entry.last_activated_at = this.now();
    state.activeTabId = tabId;
    state.activeSince = this.now();
    await this.store(state);
  }

  async recordFocusLost(): Promise<void> {
    const state = await this.load();
    this.bank(state);
    await this.store(state);
  }

  /** Build TabRecords for all live tabs; prune accounting for closed ones. */
  async snapshot(
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
        activeMs += this.now() - state.activeSince; // include the open interval
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
}
