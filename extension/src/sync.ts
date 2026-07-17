import { visitFromHistoryItem, type HistoryItemLike } from "./history";
import type { EventQueue } from "./queue";
import type { BrowserTab, TabTracker } from "./tabs";

export interface SyncDeps {
  queue: EventQueue;
  tracker: TabTracker;
  client: { notifyEnqueued(): Promise<void> };
  containerName: (cookieStoreId?: string) => Promise<string | null>;
}

export async function pushTabSnapshot(
  deps: SyncDeps & { tabsQuery: () => Promise<BrowserTab[]> },
): Promise<void> {
  const tabs = await deps.tabsQuery();
  const records = await deps.tracker.snapshot(tabs, deps.containerName);
  await deps.queue.enqueue({ type: "tab_state", tabs: records });
  await deps.client.notifyEnqueued();
}

export async function pushVisit(deps: SyncDeps, item: HistoryItemLike): Promise<void> {
  const visit = visitFromHistoryItem(item);
  if (!visit) return;
  await deps.queue.enqueue({ type: "history_visits", visits: [visit] });
  await deps.client.notifyEnqueued();
}

interface ContextualIdentitiesLike {
  get(cookieStoreId: string): Promise<{ name: string }>;
}

/** Firefox enhancement layer: resolve a tab's container name, or null on
 * Chrome / default stores / lookup failure. Results are cached per store id. */
export function makeContainerResolver(
  api: ContextualIdentitiesLike | undefined,
): (cookieStoreId?: string) => Promise<string | null> {
  const cache = new Map<string, string | null>();
  return async (cookieStoreId) => {
    if (!api || !cookieStoreId || !cookieStoreId.startsWith("firefox-container-")) return null;
    if (cache.has(cookieStoreId)) return cache.get(cookieStoreId)!;
    let name: string | null;
    try {
      name = (await api.get(cookieStoreId)).name;
    } catch {
      name = null;
    }
    cache.set(cookieStoreId, name);
    return name;
  };
}
