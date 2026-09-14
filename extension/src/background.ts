import { gapFillVisits, type HistoryApi } from "./history";
import {
  linkClickEvent,
  pageCaptureEvent,
  pageDwellEvent,
  pageScreenshotEvent,
  type ClickMessage,
  type PageMessage,
} from "./capture";
import { dataUrlToBytes, toHttpUrl, uploadBlob } from "./blobs";
import { intervalElapsed, scrollStepCount } from "./screenshot";
import { navEdgeEvent } from "./navigation";
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

let tickingTabId: number | null = null;

function tellTab(tabId: number, action: "start" | "stop"): void {
  void browser.tabs
    .sendMessage(tabId, { type: "bb:screenshot-tick-control", action })
    .catch(() => undefined); // no content script in this tab (e.g. a protected page) — fine
}

/** Ensure exactly the given tab (or none) is running the screenshot tick
 * loop. O(1) regardless of how many tabs are open — only ever one tab ticks
 * at a time, toggled on focus change. See the design spec's
 * scheduling-mechanism rationale for why this beats browser.alarms (too
 * coarse a floor for a configurable sub-minute interval) and a bare
 * background setInterval (only survives because WS heartbeat traffic
 * happens to prevent worker suspension — fragile independent of backend
 * uptime, and doesn't need to be relied on here). */
function setTickingTab(tabId: number | null): void {
  if (tickingTabId === tabId) return;
  if (tickingTabId !== null) tellTab(tickingTabId, "stop");
  tickingTabId = tabId;
  if (tabId !== null) tellTab(tabId, "start");
}

/** Derive ticking state from persisted session state (idempotent), mirroring
 * syncCaptureState. Call together with it wherever session lifecycle
 * changes (start/stop/pause/resume, wake reconcile). */
async function syncTickingState(): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status !== "recording") {
    setTickingTab(null);
    return;
  }
  const [tab] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
  setTickingTab(tab?.id ?? null);
}

interface SessionCommand {
  type: "bb:session";
  action: "start" | "pause" | "resume" | "stop" | "status" | "update-capture";
  name?: string;
  screenshotsEnabled?: boolean;
  screenshotIntervalMs?: number;
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
        const capture = {
          screenshotsEnabled: cmd.screenshotsEnabled ?? false,
          screenshotIntervalMs: cmd.screenshotIntervalMs ?? 30_000,
        };
        const { event } = await sessionManager.start(name, capture);
        await queue.enqueue(event);
        await (await ensureClient()).notifyEnqueued();
        break;
      }
      case "update-capture":
        await sessionManager.updateCapture({
          screenshotsEnabled: cmd.screenshotsEnabled ?? false,
          screenshotIntervalMs: cmd.screenshotIntervalMs ?? 30_000,
        });
        break;
      case "pause":
        // Deliberately does not touch the dwell map: dwell is not gated on
        // pause (spec §3, "deliberately not a boundary") — the underlying
        // Tier-1 clock (tabs.ts) is ambient and keeps ticking regardless.
        // Don't "fix" this by freezing/flushing here without also updating
        // the spec — see TC9 in the Slice B smoke plan.
        await sessionManager.pause();
        break;
      case "resume":
        await sessionManager.resume();
        break;
      case "stop": {
        await flushAllDwell();
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
    await syncTickingState();
  });
  return { session: await sessionManager.current(), connection: clientStatus };
}

async function handlePageMessage(
  msg: PageMessage,
  cookieStoreId: string | undefined,
  tabId: number | undefined,
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
  if (tabId !== undefined) {
    await sendDwell(await rebaseDwell(tabId, event.url));
  }
}

const NAV_URLS_KEY = "bb:nav-urls"; // per-tab last committed URL, for edge from_url

// storage.session get/set are not transactional; webNavigation events for
// different tabs interleave, so guard the read-modify-write like EventQueue
// does — a lost update would drop an edge's from_url (its whole point).
let navMutex: Promise<unknown> = Promise.resolve();
function navSerialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = navMutex.then(fn, fn);
  navMutex = next.catch(() => undefined);
  return next;
}

async function readNavUrls(): Promise<Record<number, string>> {
  return ((await session.get(NAV_URLS_KEY))[NAV_URLS_KEY] as Record<number, string>) ?? {};
}

/** Atomically record tabId's new URL and return its previous one (the edge
 * origin). */
function commitTabUrl(tabId: number, url: string): Promise<string | null> {
  return navSerialize(async () => {
    const urls = await readNavUrls();
    const prev = urls[tabId] ?? null;
    urls[tabId] = url;
    await session.set({ [NAV_URLS_KEY]: urls });
    return prev;
  });
}

/** Seed a freshly-opened tab's origin with its opener's current URL, so the
 * child's own onCommitted produces an opener→target edge. */
function seedTabUrl(tabId: number, sourceTabId: number): Promise<void> {
  return navSerialize(async () => {
    const urls = await readNavUrls();
    const opener = urls[sourceTabId];
    if (opener) {
      urls[tabId] = opener;
      await session.set({ [NAV_URLS_KEY]: urls });
    }
  });
}

function forgetTab(tabId: number): Promise<void> {
  return navSerialize(async () => {
    const urls = await readNavUrls();
    if (tabId in urls) {
      delete urls[tabId];
      await session.set({ [NAV_URLS_KEY]: urls });
    }
  });
}

const DWELL_KEY = "bb:dwell"; // per-tab { url, baseline } snapshot for dwell accounting

// Same non-transactional-storage hazard as the nav-url map above.
let dwellMutex: Promise<unknown> = Promise.resolve();
function dwellSerialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = dwellMutex.then(fn, fn);
  dwellMutex = next.catch(() => undefined);
  return next;
}

interface DwellEntry {
  url: string;
  baseline: number;
}

async function readDwellMap(): Promise<Record<number, DwellEntry>> {
  return ((await session.get(DWELL_KEY))[DWELL_KEY] as Record<number, DwellEntry>) ?? {};
}

/** Flush the previous page's dwell (if any) for this tab and start tracking
 * the new one from the tab's current active-time baseline (the Tier-1
 * focus-based tracker in tabs.ts). Returns the flushed increment, or null
 * when there was nothing to flush (first page in a fresh tab) or the
 * elapsed time was zero (tab was never focused since the last boundary). */
function rebaseDwell(
  tabId: number,
  newUrl: string,
): Promise<{ url: string; dwellMs: number } | null> {
  return dwellSerialize(async () => {
    const map = await readDwellMap();
    const prev = map[tabId];
    const now = await tracker.activeMs(tabId);
    map[tabId] = { url: newUrl, baseline: now };
    await session.set({ [DWELL_KEY]: map });
    if (!prev) return null;
    const dwellMs = Math.max(0, now - prev.baseline);
    return dwellMs > 0 ? { url: prev.url, dwellMs } : null;
  });
}

/** Flush a tab's current dwell without starting a new tracking entry — used
 * on tab close, where there is no "next page" to rebase onto. */
function flushDwell(tabId: number): Promise<{ url: string; dwellMs: number } | null> {
  return dwellSerialize(async () => {
    const map = await readDwellMap();
    const prev = map[tabId];
    if (!prev) return null;
    delete map[tabId];
    await session.set({ [DWELL_KEY]: map });
    const now = await tracker.activeMs(tabId);
    const dwellMs = Math.max(0, now - prev.baseline);
    return dwellMs > 0 ? { url: prev.url, dwellMs } : null;
  });
}

/** Flush every tracked tab's dwell — called on session stop so the final
 * page of each open tab isn't lost (nothing else triggers a rebase for it
 * once the session ends).
 *
 * Precondition: only ever called while a session is still active (from the
 * "stop" case, before `sessionManager.stop()` clears it) — the resulting
 * `page_dwell` events must reach the backend before `session_end` does, or
 * they'd be rejected as "no active session." There's no guard against a
 * future no-session callsite; if one is ever added, it must preserve that
 * ordering rather than gate on session state here. */
async function flushAllDwell(): Promise<void> {
  const map = await dwellSerialize(async () => {
    const m = await readDwellMap();
    await session.set({ [DWELL_KEY]: {} });
    return m;
  });
  for (const [tabIdStr, entry] of Object.entries(map)) {
    const now = await tracker.activeMs(Number(tabIdStr));
    const dwellMs = Math.max(0, now - entry.baseline);
    if (dwellMs > 0) await sendDwell({ url: entry.url, dwellMs });
  }
}

async function sendDwell(flush: { url: string; dwellMs: number } | null): Promise<void> {
  if (!flush) return;
  await queue.enqueue(pageDwellEvent(flush.url, flush.dwellMs));
  await (await ensureClient()).notifyEnqueued();
}

const MAX_FULLPAGE_TILES = 20;

async function captureTile(tabId: number, windowId: number): Promise<string | null> {
  let dataUrl: string;
  try {
    dataUrl = await browser.tabs.captureVisibleTab(windowId, { format: "jpeg", quality: 70 });
  } catch (e) {
    console.warn("BrowserBuddy: screenshot capture failed", e);
    return null;
  }
  const settings = await loadSettings(local);
  try {
    return await uploadBlob(toHttpUrl(settings.backendUrl), settings.secret, dataUrlToBytes(dataUrl));
  } catch (e) {
    // Best-effort, no retry buffer — see the design spec's "backend assumed
    // reachable" scope decision.
    console.warn("BrowserBuddy: screenshot upload failed", e);
    return null;
  }
}

const lastScreenshotAt = new Map<number, number>();

/** Handle a bb:screenshot-tick heartbeat from a content script. The tab is
 * necessarily the one background designated active (see setTickingTab) —
 * this re-check guards a stale tick from a tab that stopped being active a
 * moment ago, racing its own "stop" message. */
async function handleScreenshotTick(tabId: number | undefined): Promise<void> {
  if (tabId === undefined || tabId !== tickingTabId) return;
  const state = await sessionManager.current();
  if (state?.status !== "recording" || !state.screenshotsEnabled) return;
  const now = Date.now();
  const last = lastScreenshotAt.get(tabId) ?? 0;
  if (!intervalElapsed(now, last, state.screenshotIntervalMs)) return;
  // Consume this interval slot regardless of outcome: a failed capture/upload
  // must not retry on every subsequent tick (as often as every 2s) — see the
  // "no retry buffer" scope decision.
  lastScreenshotAt.set(tabId, now);
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab?.url || tab.windowId === undefined) return;
  const key = await captureTile(tabId, tab.windowId);
  if (!key) return;
  await queue.enqueue(pageScreenshotEvent(tab.url, "periodic", [key]));
  await (await ensureClient()).notifyEnqueued();
}

// Concurrent runs would interleave their scroll steps and captureVisibleTab
// calls against the same tab, stitching tiles from the wrong offsets. A
// second "Capture now" while one is running is simply ignored.
let fullPageCaptureInFlight = false;

/** On-demand full-page capture: scroll the tab in viewport-height steps
 * (driven by the content script, which alone has DOM access), capturing a
 * tile at each stop, then ship all tiles as one page_screenshot event for
 * the backend to stitch. */
async function captureFullPage(tabId: number): Promise<void> {
  if (fullPageCaptureInFlight) return;
  fullPageCaptureInFlight = true;
  try {
    const tab = await browser.tabs.get(tabId).catch(() => null);
    if (!tab?.url || tab.windowId === undefined) return;
    const first = await browser.tabs
      .sendMessage(tabId, { type: "bb:scroll-step", index: 0 })
      .catch(() => null);
    if (!first) return;
    const { scrollHeight, viewportHeight } = first as {
      scrollHeight: number;
      viewportHeight: number;
    };
    const steps = Math.min(scrollStepCount(scrollHeight, viewportHeight), MAX_FULLPAGE_TILES);
    const keys: string[] = [];
    for (let i = 0; i < steps; i++) {
      if (i > 0) {
        const ok = await browser.tabs
          .sendMessage(tabId, { type: "bb:scroll-step", index: i })
          .catch(() => null);
        if (!ok) break;
      }
      const key = await captureTile(tabId, tab.windowId);
      // A dropped tile mid-capture would ship an incomplete-but-plausible
      // stitched image; abort the whole capture instead of shipping a partial.
      if (!key) {
        keys.length = 0;
        break;
      }
      keys.push(key);
    }
    await browser.tabs
      .sendMessage(tabId, { type: "bb:scroll-step", index: -1 })
      .catch(() => undefined);
    if (keys.length === 0) return;
    await queue.enqueue(pageScreenshotEvent(tab.url, "on_demand_fullpage", keys));
    await (await ensureClient()).notifyEnqueued();
  } finally {
    fullPageCaptureInFlight = false;
  }
}

async function handleCommitted(details: {
  tabId: number;
  frameId: number;
  url: string;
  transitionType?: string;
  transitionQualifiers?: string[];
}): Promise<void> {
  if (details.frameId !== 0) return; // top-frame navigations only
  const fromUrl = await commitTabUrl(details.tabId, details.url);
  const state = await sessionManager.current();
  if (state?.status !== "recording") return;
  const event = navEdgeEvent({
    fromUrl,
    toUrl: details.url,
    transition: details.transitionType,
    qualifiers: details.transitionQualifiers,
    occurredAt: Date.now(),
  });
  if (!event) return;
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
}

async function handleClickMessage(msg: ClickMessage): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status !== "recording") return;
  const event = linkClickEvent(msg);
  if (!event) return;
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
        logged(syncTickingState());
      } else {
        scheduleSnapshot();
      }
    })
    .catch(() => scheduleSnapshot()); // window can close mid-switch
});
browser.tabs.onCreated.addListener(() => scheduleSnapshot());
browser.tabs.onRemoved.addListener((tabId) => {
  scheduleSnapshot();
  logged(forgetTab(tabId));
  // No recording-status guard here, deliberately: flushDwell/sendDwell are
  // a no-op if there's nothing tracked for this tab (map entries only ever
  // get created while recording, in handlePageMessage), and if a session
  // has since ended, the backend's existing no-active-session error frame
  // (same path every other event type uses) is the correct outcome, not a
  // bug to guard against here. A pause in progress is not excluded either
  // — see the "pause" case in handleSessionCommand for why.
  logged(flushDwell(tabId).then(sendDwell));
});
browser.webNavigation.onCommitted.addListener((details) => {
  logged(handleCommitted(details));
});
// New-tab / new-window opens: seed the child tab's from-URL with the opener's
// current URL so the child's own onCommitted produces an opener→target edge.
browser.webNavigation.onCreatedNavigationTarget.addListener((details) => {
  logged(seedTabUrl(details.tabId, details.sourceTabId));
});
browser.tabs.onUpdated.addListener((_id, changeInfo) => {
  if (changeInfo.url || changeInfo.title) scheduleSnapshot();
});
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    logged(tracker.recordFocusLost().then(scheduleSnapshot));
    setTickingTab(null); // browser itself lost focus — nothing is "the active tab"
  } else {
    logged(
      browser.tabs.query({ active: true, windowId }).then(([tab]) => {
        if (tab?.id !== undefined) {
          return tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
        }
      }),
    );
    logged(syncTickingState());
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
    logged(handlePageMessage(msg as PageMessage, sender.tab?.cookieStoreId, sender.tab?.id));
    return false; // no reply — content script fire-and-forget
  }
  if (msg?.type === "bb:click") {
    logged(handleClickMessage(message as ClickMessage));
    return false; // fire-and-forget
  }
  if (msg?.type === "bb:screenshot-tick") {
    logged(handleScreenshotTick(sender.tab?.id));
    return false;
  }
  // A fresh content script (new document after an in-tab navigation) has no
  // timer running, and setTickingTab won't re-arm it because tickingTabId
  // never changed. Re-arm here instead — no dependence on injection timing.
  if (msg?.type === "bb:screenshot-tick-hello") {
    if (sender.tab?.id !== undefined && sender.tab.id === tickingTabId) {
      tellTab(sender.tab.id, "start");
    }
    return false;
  }
  if (msg?.type === "bb:capture-fullpage") {
    logged(
      sessionManager.current().then((s) => {
        if (s?.status !== "recording" || tickingTabId === null) return;
        return captureFullPage(tickingTabId);
      }),
    );
    return false;
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

// Reconcile capture registration + badge + screenshot ticking with
// persisted session state on wake: a browser restart keeps the session
// recording (user never said stop).
logged(serializeLifecycle(() => syncCaptureState().then(syncTickingState)));
