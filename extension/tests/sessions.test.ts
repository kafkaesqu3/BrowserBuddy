import { expect, test } from "vitest";
import { SessionManager } from "@src/sessions";
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

test("stop returns a session_end event and clears state", async () => {
  const { mgr, advance } = manager();
  await mgr.start("s");
  advance(60_000);
  const result = await mgr.stop();
  expect(result?.event).toEqual({ type: "session_end", ended_at: 1_060_000 });
  expect(await mgr.current()).toBeNull();
  expect(await mgr.stop()).toBeNull(); // idempotent
});

test("start while a session is active replaces it (backend closes the old one)", async () => {
  const { mgr } = manager();
  await mgr.start("one");
  const result = await mgr.start("two");
  expect(result.event.type).toBe("session_start");
  expect((await mgr.current())?.name).toBe("two");
});
