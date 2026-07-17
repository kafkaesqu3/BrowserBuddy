import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EventQueue } from "@src/queue";
import type { EventPayload } from "@src/types";
import { WsClient } from "@src/ws";
import { FakeSocket, fakeStorageArea } from "./fakes";

const visits = (url: string): EventPayload => ({
  type: "history_visits",
  visits: [{ url, title: "", visit_time: 1720000000000 }],
});

function setup() {
  const sockets: FakeSocket[] = [];
  const queue = new EventQueue(fakeStorageArea());
  const client = new WsClient({
    url: "ws://127.0.0.1:8765/ws",
    secret: "s3cret",
    clientId: "ext-test",
    queue,
    makeSocket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
  });
  return { sockets, queue, client };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("connect authenticates, acks server-known seqs, drains the rest", async () => {
  const { sockets, queue, client } = setup();
  await queue.enqueue(visits("https://a.com")); // seq 1
  await queue.enqueue(visits("https://b.com")); // seq 2
  await queue.enqueue(visits("https://c.com")); // seq 3
  client.connect();
  const s = sockets[0]!;
  s.open();
  expect(s.sentJson()[0]).toEqual({ type: "auth", secret: "s3cret", client_id: "ext-test" });
  s.receive({ type: "auth_ok", last_seq: 2 });
  await vi.advanceTimersByTimeAsync(0);
  expect(client.status).toBe("authed");
  expect((await queue.pending()).map((e) => e.seq)).toEqual([3]); // 1,2 pruned
  const envelopes = s.sentJson().slice(1) as { seq: number }[];
  expect(envelopes.map((e) => e.seq)).toEqual([3]); // only unseen sent
});

test("ack prunes; error drops exactly the corresponding envelope", async () => {
  const { sockets, queue, client } = setup();
  await queue.enqueue(visits("https://a.com")); // seq 1
  await queue.enqueue(visits("https://b.com")); // seq 2
  client.connect();
  const s = sockets[0]!;
  s.open();
  s.receive({ type: "auth_ok", last_seq: 0 });
  await vi.advanceTimersByTimeAsync(0); // both sent, FIFO inflight [1, 2]
  s.receive({ type: "error", detail: "bad" }); // corresponds to seq 1
  s.receive({ type: "ack", seq: 2 });
  await vi.advanceTimersByTimeAsync(0);
  expect(await queue.pending()).toEqual([]); // 1 dropped, 2 acked
});

test("events enqueued while authed are pushed immediately", async () => {
  const { sockets, queue, client } = setup();
  client.connect();
  const s = sockets[0]!;
  s.open();
  s.receive({ type: "auth_ok", last_seq: 0 });
  await vi.advanceTimersByTimeAsync(0);
  await queue.enqueue(visits("https://late.com")); // seq 1
  await client.notifyEnqueued();
  const envelopes = s.sentJson().slice(1) as { seq: number }[];
  expect(envelopes.map((e) => e.seq)).toEqual([1]);
});

test("heartbeat pings keep FIFO correspondence intact", async () => {
  const { sockets, queue, client } = setup();
  client.connect();
  const s = sockets[0]!;
  s.open();
  s.receive({ type: "auth_ok", last_seq: 0 });
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(20_000); // heartbeat fires
  expect(s.sentJson()).toContainEqual({ type: "ping" });
  await queue.enqueue(visits("https://a.com")); // seq 1
  await client.notifyEnqueued();
  s.receive({ type: "pong" }); // pops the ping marker, not the envelope
  s.receive({ type: "error", detail: "bad" }); // must drop seq 1, not a ping
  await vi.advanceTimersByTimeAsync(0);
  expect(await queue.pending()).toEqual([]);
});

test("reconnects with exponential backoff and resets after auth", async () => {
  const { sockets, client } = setup();
  client.connect();
  sockets[0]!.open();
  sockets[0]!.close(); // schedules reconnect at base delay (1s)
  expect(client.status).toBe("disconnected");
  await vi.advanceTimersByTimeAsync(1000);
  expect(sockets).toHaveLength(2);
  sockets[1]!.close(); // second failure: 2s
  await vi.advanceTimersByTimeAsync(1000);
  expect(sockets).toHaveLength(2); // too early
  await vi.advanceTimersByTimeAsync(1000);
  expect(sockets).toHaveLength(3);
  sockets[2]!.open();
  sockets[2]!.receive({ type: "auth_ok", last_seq: 0 });
  await vi.advanceTimersByTimeAsync(0); // backoff reset
  sockets[2]!.close();
  await vi.advanceTimersByTimeAsync(1000);
  expect(sockets).toHaveLength(4); // back to base delay
});

test("disconnect() stops reconnection (settings changed)", async () => {
  const { sockets, client } = setup();
  client.connect();
  sockets[0]!.open();
  client.disconnect();
  await vi.advanceTimersByTimeAsync(120_000);
  expect(sockets).toHaveLength(1);
});
