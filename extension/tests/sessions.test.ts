import { expect, test } from "vitest";
import { SessionManager } from "@src/sessions";
import type { EventPayload } from "@src/types";
import { fakeStorageArea } from "./fakes";

function manager(startAt = 1_000_000) {
  let t = startAt;
  const local = fakeStorageArea();
  return {
    local,
    mgr: new SessionManager(local, () => t),
    advance: (ms: number) => (t += ms),
  };
}

test("start returns a session_start event and sets recording state", async () => {
  const { mgr } = manager();
  const result = await mgr.start("quantum papers");
  expect(result.event).toEqual({
    type: "session_start",
    name: "quantum papers",
    started_at: 1_000_000,
  });
  expect(await mgr.current()).toEqual({
    name: "quantum papers",
    startedAt: 1_000_000,
    status: "recording",
    screenshotsEnabled: false,
    screenshotIntervalMs: 30_000,
  });
});

test("state survives manager re-creation (worker death / browser restart)", async () => {
  const { mgr, local } = manager();
  await mgr.start("s");
  const revived = new SessionManager(local, () => 2_000_000);
  expect((await revived.current())?.status).toBe("recording");
});

test("pause and resume toggle status without backend events", async () => {
  const { mgr } = manager();
  await mgr.start("s");
  await mgr.pause();
  expect((await mgr.current())?.status).toBe("paused");
  await mgr.resume();
  expect((await mgr.current())?.status).toBe("recording");
});

test("stop commits the session_end event, then clears state", async () => {
  const { mgr, advance } = manager();
  await mgr.start("s");
  advance(60_000);
  const committed: EventPayload[] = [];
  const stopped = await mgr.stop(async (event) => {
    // State must still be present when commit runs (durable-enqueue-first).
    expect((await mgr.current())?.status).toBe("recording");
    committed.push(event);
  });
  expect(stopped).toBe(true);
  expect(committed).toEqual([{ type: "session_end", ended_at: 1_060_000 }]);
  expect(await mgr.current()).toBeNull();
});

test("stop with no active session returns false and never calls commit", async () => {
  const { mgr } = manager();
  let calls = 0;
  const stopped = await mgr.stop(async () => {
    calls += 1;
  });
  expect(stopped).toBe(false);
  expect(calls).toBe(0);
});

test("stop preserves session state when commit throws (no lost session_end)", async () => {
  const { mgr } = manager();
  await mgr.start("s");
  await expect(
    mgr.stop(async () => {
      throw new Error("enqueue failed");
    }),
  ).rejects.toThrow("enqueue failed");
  // Session survives so the user can retry Stop and re-send session_end.
  expect((await mgr.current())?.status).toBe("recording");
});

test("start while a session is active replaces it (backend closes the old one)", async () => {
  const { mgr } = manager();
  await mgr.start("one");
  const result = await mgr.start("two");
  expect(result.event.type).toBe("session_start");
  expect((await mgr.current())?.name).toBe("two");
});

test("start accepts explicit capture options", async () => {
  const { mgr } = manager();
  await mgr.start("s", { screenshotsEnabled: true, screenshotIntervalMs: 5000 });
  expect(await mgr.current()).toEqual({
    name: "s",
    startedAt: 1_000_000,
    status: "recording",
    screenshotsEnabled: true,
    screenshotIntervalMs: 5000,
  });
});

test("updateCapture changes settings mid-session without touching status", async () => {
  const { mgr } = manager();
  await mgr.start("s", { screenshotsEnabled: false, screenshotIntervalMs: 30_000 });
  await mgr.pause();
  await mgr.updateCapture({ screenshotsEnabled: true, screenshotIntervalMs: 10_000 });
  const state = await mgr.current();
  expect(state?.screenshotsEnabled).toBe(true);
  expect(state?.screenshotIntervalMs).toBe(10_000);
  expect(state?.status).toBe("paused"); // untouched
});

test("updateCapture is a no-op with no active session", async () => {
  const { mgr } = manager();
  await mgr.updateCapture({ screenshotsEnabled: true, screenshotIntervalMs: 1000 });
  expect(await mgr.current()).toBeNull();
});
