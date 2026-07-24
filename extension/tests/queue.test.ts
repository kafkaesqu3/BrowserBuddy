import { expect, test } from "vitest";
import { EventQueue, MAX_QUEUE } from "@src/queue";
import type { EventPayload } from "@src/types";
import { fakeStorageArea } from "./fakes";

const tabState = (url: string): EventPayload => ({
  type: "tab_state",
  tabs: [{
    tab_id: 1, window_id: 1, url, title: "", group_id: null,
    container: null, opened_at: null, last_activated_at: null, active_ms: 0,
  }],
});
const visits = (url: string): EventPayload => ({
  type: "history_visits",
  visits: [{ url, title: "", visit_time: 1720000000000 }],
});

test("enqueue assigns monotonically increasing seq and persists", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  const a = await q.enqueue(visits("https://a.com"));
  const b = await q.enqueue(visits("https://b.com"));
  expect([a.seq, b.seq]).toEqual([1, 2]);
  expect(a.v).toBe(1);
  // A fresh queue over the same storage sees the same state (worker death).
  const q2 = new EventQueue(storage);
  expect((await q2.pending()).map((e) => e.seq)).toEqual([1, 2]);
  expect((await q2.enqueue(visits("https://c.com"))).seq).toBe(3);
});

test("ack prunes everything at or below the acked seq", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com"));
  await q.enqueue(visits("https://b.com"));
  await q.enqueue(visits("https://c.com"));
  await q.ack(2);
  expect((await q.pending()).map((e) => e.seq)).toEqual([3]);
});

test("drop removes exactly one seq (server error frame = skip)", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com"));
  await q.enqueue(visits("https://b.com"));
  await q.drop(1);
  expect((await q.pending()).map((e) => e.seq)).toEqual([2]);
});

test("enqueue coalesces pending tab_state (only latest snapshot matters)", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(tabState("https://old.com"));
  await q.enqueue(visits("https://v.com"));
  const latest = await q.enqueue(tabState("https://new.com"));
  const pending = await q.pending();
  const tabEvents = pending.filter((e) => e.event.type === "tab_state");
  expect(tabEvents).toHaveLength(1);
  expect(tabEvents[0]!.seq).toBe(latest.seq);
  // history_visits are never coalesced
  expect(pending.some((e) => e.event.type === "history_visits")).toBe(true);
});

test("queue is capped: oldest events drop beyond MAX_QUEUE", async () => {
  const storage = fakeStorageArea();
  // Seed a full queue directly in the per-event layout (enqueueing MAX_QUEUE
  // events one by one would be O(n^2)).
  const index = Array.from({ length: MAX_QUEUE }, (_, i) => ({
    seq: i + 1,
    type: "history_visits",
  }));
  const bodies: Record<string, unknown> = {};
  for (const e of index) {
    bodies[`bb:q:${e.seq}`] = { v: 1, seq: e.seq, event: visits(`https://p${e.seq}.com`) };
  }
  await storage.set({ "bb:seq": MAX_QUEUE, "bb:queue:index": index, ...bodies });
  const q = new EventQueue(storage);
  for (let i = 0; i < 10; i++) await q.enqueue(visits(`https://q${i}.com`)); // seq 5001..5010
  const pending = await q.pending();
  expect(pending).toHaveLength(MAX_QUEUE);
  expect(pending[0]!.seq).toBe(11); // 10 oldest dropped
});

test("concurrent enqueues produce unique seq numbers (no RMW race)", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  // Start both before either resolves — simulates two event listeners firing.
  const [a, b] = await Promise.all([
    q.enqueue(visits("https://a.com")),
    q.enqueue(visits("https://b.com")),
  ]);
  expect(new Set([a.seq, b.seq]).size).toBe(2); // must be distinct
  expect(await q.pending()).toHaveLength(2); // neither event lost
});

test("ack concurrent with enqueue does not lose the new event", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com")); // seq 1
  await q.enqueue(visits("https://b.com")); // seq 2
  await Promise.all([q.ack(1), q.enqueue(visits("https://c.com"))]);
  const seqs = (await q.pending()).map((e) => e.seq);
  expect(seqs).toContain(3); // event c must not be lost
  expect(seqs).not.toContain(1); // and the stale ack write must not resurrect already-acked seqs
});

test("enqueue stores each event under its own bb:q:<seq> key", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com")); // seq 1
  await q.enqueue(visits("https://b.com")); // seq 2
  expect(storage.data.has("bb:q:1")).toBe(true);
  expect(storage.data.has("bb:q:2")).toBe(true);
  // Legacy single-array key is not used.
  expect(storage.data.has("bb:queue")).toBe(false);
  const index = storage.data.get("bb:queue:index") as { seq: number }[];
  expect(index.map((e) => e.seq)).toEqual([1, 2]);
});

test("ack removes the acked bodies, not just the index entries", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com")); // seq 1
  await q.enqueue(visits("https://b.com")); // seq 2
  await q.ack(1);
  expect(storage.data.has("bb:q:1")).toBe(false); // body reclaimed
  expect(storage.data.has("bb:q:2")).toBe(true);
  expect((await q.pending()).map((e) => e.seq)).toEqual([2]);
});

test("migrates a legacy bb:queue array to per-event keys on first use", async () => {
  const storage = fakeStorageArea();
  await storage.set({
    "bb:seq": 2,
    "bb:queue": [
      { v: 1, seq: 1, event: visits("https://a.com") },
      { v: 1, seq: 2, event: visits("https://b.com") },
    ],
  });
  const q = new EventQueue(storage);
  expect((await q.pending()).map((e) => e.seq)).toEqual([1, 2]);
  expect(storage.data.has("bb:queue")).toBe(false); // legacy key removed
  expect(storage.data.has("bb:q:1")).toBe(true);
  // seq counter is preserved across the migration.
  expect((await q.enqueue(visits("https://c.com"))).seq).toBe(3);
});

test("a fresh queue with no legacy data starts empty and assigns seq 1", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  expect(await q.pending()).toEqual([]);
  expect(storage.data.has("bb:queue")).toBe(false); // no spurious legacy write
  expect((await q.enqueue(visits("https://a.com"))).seq).toBe(1);
});

test("an empty legacy bb:queue array is cleaned up without creating an index", async () => {
  const storage = fakeStorageArea();
  await storage.set({ "bb:queue": [] });
  const q = new EventQueue(storage);
  expect(await q.pending()).toEqual([]);
  expect(storage.data.has("bb:queue")).toBe(false); // legacy key removed
});
