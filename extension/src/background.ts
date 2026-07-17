import { gapFillVisits, type HistoryApi } from "./history";
import { EventQueue } from "./queue";
import { loadSettings } from "./settings";
import { makeContainerResolver, pushTabSnapshot, pushVisit } from "./sync";
import { TabTracker, type BrowserTab } from "./tabs";
import type { StorageArea } from "./types";
import { WsClient, type WsLike } from "./ws";

const local = browser.storage.local as unknown as StorageArea;
const session = browser.storage.session as unknown as StorageArea;

const queue = new EventQueue(local);
const tracker = new TabTracker(session, Date.now);
const containerName = makeContainerResolver(
  // Firefox-only API; undefined on Chrome (enhancement layer).
  (globalThis as Record<string, unknown>)["browser"] !== undefined
    ? ((globalThis as Record<string, unknown>)["browser"] as Record<string, unknown>)[
        "contextualIdentities"
      ] as Parameters<typeof makeContainerResolver>[0]
    : undefined,
);

let client: WsClient | null = null;

async function ensureClient(): Promise<WsClient> {
  if (client) return client;
  const settings = await loadSettings(local);
  client = new WsClient({
    url: settings.backendUrl,
    secret: settings.secret,
    clientId: settings.clientId,
    queue,
    // M3 hardening from the ws.ts review: an invalid user-edited URL must not
    // leave the client wedged in "connecting" — treat construction failure
    // like a connection failure (status stays disconnected; alarm retries).
    makeSocket: (url) => new WebSocket(url) as unknown as WsLike,
  });
  try {
    client.connect();
  } catch (err) {
    console.warn("BrowserBuddy: cannot open WebSocket (bad URL?)", err);
  }
  return client;
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
    void syncDeps().then(pushTabSnapshot);
  }, 2000);
}

browser.tabs.onActivated.addListener((info) => {
  void browser.windows.get(info.windowId).then((win) => {
    if (win.focused) {
      void tracker.recordActivation(info.tabId, info.windowId).then(scheduleSnapshot);
    } else {
      scheduleSnapshot();
    }
  });
});
browser.tabs.onCreated.addListener(() => scheduleSnapshot());
browser.tabs.onRemoved.addListener(() => scheduleSnapshot());
browser.tabs.onUpdated.addListener((_id, changeInfo) => {
  if (changeInfo.url || changeInfo.title) scheduleSnapshot();
});
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    void tracker.recordFocusLost().then(scheduleSnapshot);
  } else {
    void browser.tabs.query({ active: true, windowId }).then(([tab]) => {
      if (tab?.id !== undefined) {
        void tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
      }
    });
  }
});

browser.history.onVisited.addListener((item) => {
  void syncDeps().then((d) => pushVisit(d, item));
});

// Periodic safety net: refresh active_ms, reconnect if needed, fill gaps.
browser.alarms.create("bb-sync", { periodInMinutes: 1 });
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "bb-sync") return;
  void (async () => {
    const d = await syncDeps();
    const visits = await gapFillVisits(
      browser.history as unknown as HistoryApi,
      local,
    );
    for (const v of visits) {
      await queue.enqueue({ type: "history_visits", visits: [v] });
    }
    await d.client.notifyEnqueued();
    await pushTabSnapshot(d);
  })();
});

// Options page asks for connection status.
browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message === "bb:status") {
    sendResponse({ status: client?.status ?? "disconnected" });
  }
});

// Settings changed: rebuild the client with the new URL/secret.
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["bb:settings"]) {
    client?.disconnect();
    client = null;
    void ensureClient();
  }
});

void ensureClient();
