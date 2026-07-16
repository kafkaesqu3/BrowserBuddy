import { PROTOCOL_VERSION } from "./types";
import type { EventEnvelope, EventPayload, StorageArea } from "./types";

const SEQ_KEY = "bb:seq";
const QUEUE_KEY = "bb:queue";

// Bound on buffered events while the backend is unreachable. History events
// beyond this are lost (oldest first); tab_state is always coalesced to one.
export const MAX_QUEUE = 5000;

/** Durable outbound queue. Seq numbers survive background-worker death. */
export class EventQueue {
  constructor(private storage: StorageArea) {}

  async enqueue(event: EventPayload): Promise<EventEnvelope> {
    const state = await this.storage.get([SEQ_KEY, QUEUE_KEY]);
    const seq = ((state[SEQ_KEY] as number) ?? 0) + 1;
    let queue = ((state[QUEUE_KEY] as EventEnvelope[]) ?? []);
    if (event.type === "tab_state") {
      queue = queue.filter((e) => e.event.type !== "tab_state");
    }
    const envelope: EventEnvelope = { v: PROTOCOL_VERSION, seq, event };
    queue.push(envelope);
    if (queue.length > MAX_QUEUE) queue = queue.slice(queue.length - MAX_QUEUE);
    await this.storage.set({ [SEQ_KEY]: seq, [QUEUE_KEY]: queue });
    return envelope;
  }

  async pending(): Promise<EventEnvelope[]> {
    const state = await this.storage.get(QUEUE_KEY);
    return ((state[QUEUE_KEY] as EventEnvelope[]) ?? []).sort((a, b) => a.seq - b.seq);
  }

  /** Remove everything at or below `seq` (server acked it, or reported it as
   * already-known via auth_ok.last_seq). */
  async ack(seq: number): Promise<void> {
    const queue = (await this.pending()).filter((e) => e.seq > seq);
    await this.storage.set({ [QUEUE_KEY]: queue });
  }

  /** Remove exactly one seq: the server sent an error frame for it, which
   * means "skip or fix, never blind-retry" (see backend EventEnvelope docs). */
  async drop(seq: number): Promise<void> {
    const queue = (await this.pending()).filter((e) => e.seq !== seq);
    await this.storage.set({ [QUEUE_KEY]: queue });
  }
}
