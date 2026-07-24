import { gapFillVisits, type HistoryApi } from "./history";
import { pageCaptureEvent, type PageMessage } from "./capture";
import { EventQueue } from "./queue";
import { loadSettings } from "./settings";
import { SessionManager } from "./sessions";
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
const sessionManager = new SessionManager(local, Date.now);
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

// ---------------------------------------------------------------------------
// Session lifecycle + dynamic content script registration
// ---------------------------------------------------------------------------

const CAPTURE_SCRIPT_ID = "bb-capture";

async function registerCapture(): Promise<void> {
  const existing = await browser.scripting.getRegisteredContentScripts({
    ids: [CAPTURE_SCRIPT_ID],
  });
  if (existing.length === 0) {
    await browser.scripting.registerContentScripts([
      {
        id: CAPTURE_SCRIPT_ID,
        js: ["content.js"],
        matches: ["<all_urls>"],
        runAt: "document_idle",
        persistAcrossSessions: true,
      },
    ]);
  }
}

async function unregisterCapture(): Promise<void> {
  try {
    await browser.scripting.unregisterContentScripts({ ids: [CAPTURE_SCRIPT_ID] });
  } catch {
    // not registered — fine
  }
}

function setBadge(status: "recording" | "paused" | null): void {
  const text = status === "recording" ? "REC" : status === "paused" ? "II" : "";
  void browser.action.setBadgeText({ text });
  void browser.action.setBadgeBackgroundColor({
    color: status === "recording" ? "#c0392b" : "#7f8c8d",
  });
}

interface SessionCommand {
  type: "bb:session";
  action: "start" | "pause" | "resume" | "stop" | "status";
  name?: string;
}

// One mutex for lifecycle side effects (capture registration + badge): the
// wake-reconciliation IIFE and popup commands race on cold starts otherwise
// (a popup click is often what wakes the worker).
let lifecycleMutex: Promise<unknown> = Promise.resolve();
function serializeLifecycle<T>(fn: () => Promise<T>): Promise<T> {
  const next = lifecycleMutex.then(fn, fn);
  lifecycleMutex = next.catch(() => undefined);
  return next;
}

/** Derive registration + badge from persisted session state (idempotent). */
async function syncCaptureState(): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status === "recording") {
    await registerCapture();
  } else {
    await unregisterCapture();
  }
  setBadge(state?.status ?? null);
}

async function handleSessionCommand(
  cmd: SessionCommand,
): Promise<{ session: unknown; connection: string }> {
  await serializeLifecycle(async () => {
    switch (cmd.action) {
      case "start": {
        // `||` not `??`: an empty/whitespace name must fall back, or the
        // backend's min_length=1 validation error-drops the event and every
        // capture behind it.
        const name = cmd.name?.trim() || "untitled session";
        const { event } = await sessionManager.start(name);
        await queue.enqueue(event);
        await (await ensureClient()).notifyEnqueued();
        break;
      }
      case "pause":
        await sessionManager.pause();
        break;
      case "resume":
        await sessionManager.resume();
        break;
      case "stop": {
        await sessionManager.stop(async (event) => {
          await queue.enqueue(event);
          await (await ensureClient()).notifyEnqueued();
        });
        break;
      }
      case "status":
        break;
    }
    await syncCaptureState();
  });
  return { session: await sessionManager.current(), connection: clientStatus };
}

async function handlePageMessage(
  msg: PageMessage,
  cookieStoreId: string | undefined,
): Promise<void> {
  const container = await containerName(cookieStoreId);
  const event = pageCaptureEvent(msg, container);
  if (!event) return;
  // Recording check happens LAST, immediately before enqueue — a stop command
  // racing this handler must not slip a capture in behind the session_end event
  // (narrows the check-to-enqueue window to zero awaits).
  const state = await sessionManager.current();
  if (state?.status !== "recording") return; // stale content script or paused
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
}

// ---------------------------------------------------------------------------
// Tab / window / history listeners
// ---------------------------------------------------------------------------

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

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message === "bb:status") {
    sendResponse({ status: clientStatus });
    return true;
  }
  const msg = message as { type?: string };
  if (msg?.type === "bb:page") {
    logged(handlePageMessage(msg as PageMessage, sender.tab?.cookieStoreId));
    return false; // no reply — content script fire-and-forget
  }
  if (msg?.type === "bb:session") {
    handleSessionCommand(msg as SessionCommand).then(sendResponse, async (e) => {
      console.warn("BrowserBuddy: session command failed", e);
      const session = await sessionManager.current().catch(() => null);
      sendResponse({ session, connection: clientStatus });
    });
    return true; // async reply — popup must never hang
  }
  return false;
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

// Reconcile capture registration + badge with persisted session state on
// wake: a browser restart keeps the session recording (user never said stop).
logged(serializeLifecycle(syncCaptureState));
