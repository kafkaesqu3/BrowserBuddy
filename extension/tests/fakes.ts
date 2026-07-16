import type { StorageArea } from "../src/types";

export function fakeStorageArea(): StorageArea & { data: Map<string, unknown> } {
  const data = new Map<string, unknown>();
  return {
    data,
    async get(keys) {
      const names = Array.isArray(keys) ? keys : [keys];
      const out: Record<string, unknown> = {};
      for (const k of names) if (data.has(k)) out[k] = data.get(k);
      return out;
    },
    async set(items) {
      for (const [k, v] of Object.entries(items)) data.set(k, v);
    },
    async remove(keys) {
      const names = Array.isArray(keys) ? keys : [keys];
      for (const k of names) data.delete(k);
    },
  };
}
