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
  // Seed a full queue directly: enqueueing MAX_QUEUE events one by one is
  // O(n^2) now that the fake clones stored values like the real API does.
  const seeded = Array.from({ length: MAX_QUEUE }, (_, i) => ({
    v: 1, seq: i + 1, event: visits(`https://p${i}.com`),
  }));
  await storage.set({ "bb:seq": MAX_QUEUE, "bb:queue": seeded });
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
