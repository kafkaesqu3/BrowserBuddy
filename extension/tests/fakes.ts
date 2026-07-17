import type { StorageArea } from "@src/types";
import type { WsLike } from "@src/ws";

export function fakeStorageArea(): StorageArea & { data: Map<string, unknown> } {
  const data = new Map<string, unknown>();
  return {
    data,
    // Real browser.storage serializes on set and returns fresh copies on get.
    // Clone both ways so callers can't share live references through storage.
    async get(keys) {
      const names = Array.isArray(keys) ? keys : [keys];
      const out: Record<string, unknown> = {};
      for (const k of names) if (data.has(k)) out[k] = structuredClone(data.get(k));
      return out;
    },
    async set(items) {
      for (const [k, v] of Object.entries(items)) data.set(k, structuredClone(v));
    },
    async remove(keys) {
      const names = Array.isArray(keys) ? keys : [keys];
      for (const k of names) data.delete(k);
    },
  };
}

export class FakeSocket implements WsLike {
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.onclose?.();
  }
  // test helpers
  open(): void {
    this.onopen?.();
  }
  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  sentJson(): unknown[] {
    return this.sent.map((s) => JSON.parse(s));
  }
}
