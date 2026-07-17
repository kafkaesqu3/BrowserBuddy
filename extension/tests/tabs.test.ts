import { expect, test } from "vitest";
import { TabTracker, type BrowserTab } from "@src/tabs";
import { fakeStorageArea } from "./fakes";

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

const tab = (id: number, url: string, extra: Partial<BrowserTab> = {}): BrowserTab => ({
  id, windowId: 1, url, title: `t${id}`, ...extra,
});

const noContainer = async () => null;

test("active time accumulates for the focused tab only", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(1, 1);
  c.advance(5000);
  await tracker.recordActivation(2, 1); // switch away: tab 1 banked 5s
  c.advance(3000);
  const records = await tracker.snapshot([tab(1, "https://a.com"), tab(2, "https://b.com")], noContainer);
  const byId = new Map(records.map((r) => [r.tab_id, r]));
  expect(byId.get(1)!.active_ms).toBe(5000);
  expect(byId.get(2)!.active_ms).toBe(3000); // still active: counted up to now
  expect(byId.get(1)!.last_activated_at).toBe(1_000_000);
  expect(byId.get(2)!.last_activated_at).toBe(1_005_000);
});

test("focus lost stops the clock", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(1, 1);
  c.advance(2000);
  await tracker.recordFocusLost();
  c.advance(60_000); // user is away: must not count
  const records = await tracker.snapshot([tab(1, "https://a.com")], noContainer);
  expect(records[0]!.active_ms).toBe(2000);
});

test("state survives tracker re-creation (worker death)", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const t1 = new TabTracker(session, c.now);
  await t1.recordActivation(1, 1);
  c.advance(4000);
  const t2 = new TabTracker(session, c.now); // fresh instance, same storage
  await t2.recordActivation(2, 1);
  const records = await t2.snapshot([tab(1, "https://a.com"), tab(2, "https://b.com")], noContainer);
  expect(records.find((r) => r.tab_id === 1)!.active_ms).toBe(4000);
});

test("snapshot prunes state for closed tabs and fills metadata", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(99, 1); // tab that will be gone
  const records = await tracker.snapshot(
    [tab(1, "https://a.com", { groupId: 7, cookieStoreId: "firefox-container-1" })],
    async (storeId) => (storeId === "firefox-container-1" ? "Research" : null),
  );
  expect(records).toHaveLength(1);
  expect(records[0]!.group_id).toBe(7);
  expect(records[0]!.container).toBe("Research");
  expect(records[0]!.opened_at).toBe(1_000_000); // first-seen fallback
  // pruned: tab 99 no longer in stored accounting
  const stored = await session.get("bb:tabstate");
  expect(JSON.stringify(stored)).not.toContain("99");
});

test("interleaved snapshot and activation do not lose banked time", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(1, 1);
  c.advance(5000);
  // A snapshot (2s debounce) races a tab switch; the switch's banking must survive.
  await Promise.all([
    tracker.snapshot([tab(1, "https://a.com"), tab(2, "https://b.com")], noContainer),
    tracker.recordActivation(2, 1),
  ]);
  c.advance(3000);
  const records = await tracker.snapshot(
    [tab(1, "https://a.com"), tab(2, "https://b.com")],
    noContainer,
  );
  const byId = new Map(records.map((r) => [r.tab_id, r]));
  expect(byId.get(1)!.active_ms).toBe(5000); // banked at the switch, not double-counted
  expect(byId.get(2)!.active_ms).toBe(3000);
});

test("tabs without id or url are skipped; groupId -1 maps to null", async () => {
  const c = clock();
  const tracker = new TabTracker(fakeStorageArea(), c.now);
  const records = await tracker.snapshot(
    [tab(1, "https://a.com", { groupId: -1 }), { windowId: 1, title: "no id" }, tab(2, "")],
    noContainer,
  );
  expect(records).toHaveLength(1);
  expect(records[0]!.group_id).toBeNull();
});

test("wall-clock regression cannot bank negative active time", async () => {
  let t = 1_000_000;
  const tracker = new TabTracker(fakeStorageArea(), () => t);
  await tracker.recordActivation(1, 1);
  t -= 5000; // clock went backwards (NTP correction)
  await tracker.recordFocusLost(); // banks now() - activeSince = -5000 without a clamp
  const records = await tracker.snapshot([tab(1, "https://a.com")], noContainer);
  expect(records[0]!.active_ms).toBe(0); // clamped, not negative (backend rejects ge<0)
});

test("wall-clock regression during open interval cannot produce negative active_ms in snapshot", async () => {
  let t = 1_000_000;
  const tracker = new TabTracker(fakeStorageArea(), () => t);
  await tracker.recordActivation(1, 1);
  t -= 5000; // regression while tab is still active (no recordFocusLost)
  const records = await tracker.snapshot([tab(1, "https://a.com")], noContainer);
  expect(records[0]!.active_ms).toBe(0); // snapshotImpl clamp, not bank()
});
