import { expect, test } from "vitest";
import { DEFAULT_BACKEND_URL, loadSettings, saveSettings } from "@/settings";
import { fakeStorageArea } from "@tests/fakes";

test("first load generates a stable clientId and defaults", async () => {
  const storage = fakeStorageArea();
  const first = await loadSettings(storage);
  expect(first.backendUrl).toBe(DEFAULT_BACKEND_URL);
  expect(first.secret).toBe("");
  expect(first.clientId).toMatch(/^ext-[0-9a-f-]{36}$/);
  const second = await loadSettings(storage);
  expect(second.clientId).toBe(first.clientId); // persisted, not regenerated
});

test("saveSettings patches url and secret but never clientId", async () => {
  const storage = fakeStorageArea();
  const before = await loadSettings(storage);
  await saveSettings(storage, { backendUrl: "ws://127.0.0.1:9999/ws", secret: "s3cret" });
  const after = await loadSettings(storage);
  expect(after.backendUrl).toBe("ws://127.0.0.1:9999/ws");
  expect(after.secret).toBe("s3cret");
  expect(after.clientId).toBe(before.clientId);
});
