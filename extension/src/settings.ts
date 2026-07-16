import type { StorageArea } from "@/types";

export const DEFAULT_BACKEND_URL = "ws://127.0.0.1:8765/ws";

export interface Settings {
  backendUrl: string;
  secret: string;
  clientId: string;
}

const KEY = "bb:settings";

export async function loadSettings(storage: StorageArea): Promise<Settings> {
  const stored = (await storage.get(KEY))[KEY] as Partial<Settings> | undefined;
  const settings: Settings = {
    backendUrl: stored?.backendUrl ?? DEFAULT_BACKEND_URL,
    secret: stored?.secret ?? "",
    clientId: stored?.clientId ?? `ext-${crypto.randomUUID()}`,
  };
  if (!stored?.clientId) await storage.set({ [KEY]: settings });
  return settings;
}

export async function saveSettings(
  storage: StorageArea,
  patch: Partial<Pick<Settings, "backendUrl" | "secret">>,
): Promise<void> {
  const current = await loadSettings(storage);
  await storage.set({ [KEY]: { ...current, ...patch } });
}
