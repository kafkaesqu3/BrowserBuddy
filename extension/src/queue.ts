import { PROTOCOL_VERSION } from "./types";
import type { EventEnvelope, EventPayload, StorageArea } from "./types";

const SEQ_KEY = "bb:seq";
const INDEX_KEY = "bb:queue:index";
const LEGACY_QUEUE_KEY = "bb:queue";
const bodyKey = (seq: number): string => `bb:q:${seq}`;

// Bound on buffered events while the backend is unreachable. History events
// beyond this are lost (oldest first); tab_state is always coalesced to one.
export const MAX_QUEUE = 5000;

/** Lightweight per-event index entry — the heavy envelope body lives under
 * bb:q:<seq>, so enqueue/ack/drop never rewrite unrelated bodies. */
interface IndexEntry {
  seq: number;
  type: string;
}

/** Durable outbound queue. Seq numbers survive background-worker death.
 * Layout: bb:seq (counter), bb:queue:index ([{seq,type}]), bb:q:<seq> (one
 * EventEnvelope each). One event body is written per enqueue — a queued 2 MB
 * page capture is no longer rewritten on every subsequent enqueue/ack. */
export class EventQueue {
  // storage.get/set are not transactional; interleaved listeners would race
  // the read-modify-write without this.
  private mutex: Promise<unknown> = Promise.resolve();
  private migrated = false;

  constructor(private storage: StorageArea) {}

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.mutex.then(fn, fn);
    this.mutex = next.catch(() => undefined);
    return next;
  }

  /** One-time upgrade from the legacy single-array layout. Runs inside the
   * mutex (callers already hold it) so it never re-serializes. */
  private async migrateIfNeeded(): Promise<void> {
    if (this.migrated) return;
    const legacy = (await this.storage.get(LEGACY_QUEUE_KEY))[LEGACY_QUEUE_KEY] as
      | EventEnvelope[]
      | undefined;
    if (legacy !== undefined) {
      if (legacy.length > 0) {
        const index: IndexEntry[] = [];
        const items: Record<string, unknown> = {};
        for (const env of legacy) {
          index.push({ seq: env.seq, type: env.event.type });
          items[bodyKey(env.seq)] = env;
        }
        items[INDEX_KEY] = index;
        await this.storage.set(items);
      }
      // Only touch storage when a legacy key actually exists — avoids a
      // redundant remove on every fresh worker's first queue op.
      await this.storage.remove(LEGACY_QUEUE_KEY);
    }
    this.migrated = true;
  }

  private async readIndex(): Promise<IndexEntry[]> {
    return ((await this.storage.get(INDEX_KEY))[INDEX_KEY] as IndexEntry[]) ?? [];
  }

  private async readBodies(index: IndexEntry[]): Promise<EventEnvelope[]> {
    if (index.length === 0) return [];
    const state = await this.storage.get(index.map((e) => bodyKey(e.seq)));
    return index
      .map((e) => state[bodyKey(e.seq)] as EventEnvelope | undefined)
      .filter((e): e is EventEnvelope => e !== undefined)
      .sort((a, b) => a.seq - b.seq);
  }

  enqueue(event: EventPayload): Promise<EventEnvelope> {
    return this.serialize(() => this.enqueueImpl(event));
  }

  private async enqueueImpl(event: EventPayload): Promise<EventEnvelope> {
    await this.migrateIfNeeded();
    const seqState = await this.storage.get(SEQ_KEY);
    const seq = ((seqState[SEQ_KEY] as number) ?? 0) + 1;
    let index = await this.readIndex();
    const removals: string[] = [];
    if (event.type === "tab_state") {
      index = index.filter((e) => {
        if (e.type === "tab_state") {
          removals.push(bodyKey(e.seq));
          return false;
        }
        return true;
      });
    }
    const envelope: EventEnvelope = { v: PROTOCOL_VERSION, seq, event };
    index.push({ seq, type: event.type });
    while (index.length > MAX_QUEUE) {
      const dropped = index.shift();
      if (dropped) removals.push(bodyKey(dropped.seq));
    }
    await this.storage.set({ [SEQ_KEY]: seq, [INDEX_KEY]: index, [bodyKey(seq)]: envelope });
    // Bodies are written before their stale siblings are removed; a worker
    // crash in between orphans a few bb:q:<seq> bodies (unreferenced by the
    // index, so pending() stays correct). Bounded and cosmetic — storage.local
    // has no transaction to close the window.
    if (removals.length > 0) await this.storage.remove(removals);
    return envelope;
  }

  pending(): Promise<EventEnvelope[]> {
    return this.serialize(async () => {
      await this.migrateIfNeeded();
      return this.readBodies(await this.readIndex());
    });
  }

  /** Remove everything at or below `seq` (server acked it, or reported it as
   * already-known via auth_ok.last_seq). */
  ack(seq: number): Promise<void> {
    return this.serialize(() => this.ackImpl(seq));
  }

  private async ackImpl(seq: number): Promise<void> {
    await this.migrateIfNeeded();
    const kept: IndexEntry[] = [];
    const removals: string[] = [];
    for (const e of await this.readIndex()) {
      if (e.seq <= seq) removals.push(bodyKey(e.seq));
      else kept.push(e);
    }
    await this.storage.set({ [INDEX_KEY]: kept });
    if (removals.length > 0) await this.storage.remove(removals);
  }

  /** Remove exactly one seq: the server sent an error frame for it, which
   * means "skip or fix, never blind-retry" (see backend EventEnvelope docs). */
  drop(seq: number): Promise<void> {
    return this.serialize(() => this.dropImpl(seq));
  }

  private async dropImpl(seq: number): Promise<void> {
    await this.migrateIfNeeded();
    const index = await this.readIndex();
    const kept = index.filter((e) => e.seq !== seq);
    if (kept.length === index.length) return; // nothing matched
    await this.storage.set({ [INDEX_KEY]: kept });
    await this.storage.remove(bodyKey(seq));
  }
}
