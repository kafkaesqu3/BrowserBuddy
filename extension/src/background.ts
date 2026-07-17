import { gapFillVisits, type HistoryApi } from "./history";
import { EventQueue } from "./queue";
import { loadSettings } from "./settings";
import { makeContainerResolver, pushTabSnapshot, pushVisit } from "./sync";
import { TabTracker, type BrowserTab } from "./tabs";
import type { StorageArea } from "./types";
import { WsClient, type WsLike, type WsStatus } from "./ws";

// Chrome has no `browser` global; alias it (portability policy: chrome-compatible core).
globalThis.browser ??= (globalThis as { chrome?: typeof browser }).chrome as typeof browser;

const local = browser.storage.local as unknown as StorageArea;
const session = browser.storage.session as unknown as StorageArea;

const queue = new EventQueue(local);
const tracker = new TabTracker(session, Date.now);
const containerName = makeContainerResolver(
  // Firefox-only API; undefined on Chrome (enhancement layer).
  (browser as { contextualIdentities?: Parameters<typeof makeContainerResolver>[0] })
    .contextualIdentities,
);

const logged = (p: Promise<unknown>): void =>
  void p.catch((e) => console.warn("BrowserBuddy:", e));

// Single-flight: concurrent callers must share one WsClient. Two clients would
// double-drain the queue, and the orphan's heartbeat would keep the MV3 worker
// alive forever.
let clientPromise: Promise<WsClient> | null = null;
let clientStatus: WsStatus = "disconnected";

async function buildClient(): Promise<WsClient> {
  const settings = await loadSettings(local);
  const c = new WsClient({
    url: settings.backendUrl,
    secret: settings.secret,
    clientId: settings.clientId,
    queue,
    makeSocket: (url) => new WebSocket(url) as unknown as WsLike,
    onStatus: (s) => (clientStatus = s),
  });
  c.connect();
  return c;
}

function ensureClient(): Promise<WsClient> {
  if (!clientPromise) {
    const p: Promise<WsClient> = buildClient().catch((e) => {
      if (clientPromise === p) clientPromise = null; // don't clobber a newer build
      throw e;
    });
    clientPromise = p;
  }
  return clientPromise;
}

const syncDeps = async () => ({
  queue,
  tracker,
  client: await ensureClient(),
  containerName,
  tabsQuery: () => browser.tabs.query({}) as Promise<BrowserTab[]>,
});

let snapshotTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleSnapshot(): void {
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(() => {
    logged(syncDeps().then(pushTabSnapshot));
  }, 2000);
}

browser.tabs.onActivated.addListener((info) => {
  void browser.windows
    .get(info.windowId)
    .then((win) => {
      // tabs.onActivated fires for background windows too; TabTracker's
      // precondition is "focused window's active tab only".
      if (win.focused) {
        void tracker.recordActivation(info.tabId, info.windowId).then(scheduleSnapshot);
      } else {
        scheduleSnapshot();
      }
    })
    .catch(() => scheduleSnapshot()); // window can close mid-switch
});
browser.tabs.onCreated.addListener(() => scheduleSnapshot());
browser.tabs.onRemoved.addListener(() => scheduleSnapshot());
browser.tabs.onUpdated.addListener((_id, changeInfo) => {
  if (changeInfo.url || changeInfo.title) scheduleSnapshot();
});
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    logged(tracker.recordFocusLost().then(scheduleSnapshot));
  } else {
    logged(
      browser.tabs.query({ active: true, windowId }).then(([tab]) => {
        if (tab?.id !== undefined) {
          return tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
        }
      }),
    );
  }
});

browser.history.onVisited.addListener((item) => {
  logged(syncDeps().then((d) => pushVisit(d, item)));
});

// Periodic safety net: refresh active_ms, reconnect if needed, fill gaps.
// Only create the alarm if absent so worker wakeups don't reset its phase.
logged(
  browser.alarms.get("bb-sync").then((existing) => {
    if (!existing) browser.alarms.create("bb-sync", { periodInMinutes: 1 });
  }),
);
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "bb-sync") return;
  logged(
    (async () => {
      const d = await syncDeps();
      const visits = await gapFillVisits(browser.history as unknown as HistoryApi, local);
      // Chunked batches: bounds frame size and avoids O(n^2) queue rewrites.
      for (let i = 0; i < visits.length; i += 500) {
        await queue.enqueue({ type: "history_visits", visits: visits.slice(i, i + 500) });
      }
      await d.client.notifyEnqueued();
      await pushTabSnapshot(d);
    })(),
  );
});

// Options page asks for connection status.
browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message === "bb:status") {
    sendResponse({ status: clientStatus });
    return true;
  }
});

// Settings changed: rebuild the client with the new URL/secret.
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["bb:settings"]) {
    const old = clientPromise;
    clientPromise = null;
    logged(old?.then((c) => c.disconnect()) ?? Promise.resolve());
    logged(ensureClient());
  }
});

void ensureClient();
