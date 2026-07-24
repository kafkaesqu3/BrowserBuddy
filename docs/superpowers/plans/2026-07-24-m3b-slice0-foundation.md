# BrowserBuddy M3b Slice 0 (Foundation & smoke debt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three present-need fixes with no new capture surface — a popup Settings link (discoverable pairing), a client-side durability fix for the dangling-session bug, and per-event EventQueue storage so a queued 2 MB HTML body is no longer rewritten on every enqueue/ack.

**Architecture:** All three are extension-only. The Settings link is popup UI wiring to `browser.runtime.openOptionsPage()`. The reconcile inverts `SessionManager.stop()` so local state is cleared only after `session_end` is durably enqueued (via an injected commit callback); the background passes an enqueue-and-notify callback. The EventQueue moves from one `bb:queue` array to a lightweight `bb:queue:index` (`{seq,type}[]`) plus one `bb:q:<seq>` body key per event, with a one-time migration from the legacy array.

**Tech Stack:** TypeScript, Vitest, existing `StorageArea` facade over `browser.storage.local`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-24-m3b-slice0-foundation-design.md`

Conventions unchanged: ms epoch UTC timestamps; imports never `../` (use `@src/*` in tests); imperative commit subjects, no AI attribution. Extension gates: `npm run check` (tsc + eslint + vitest) and `npm run build`. Baseline before this plan: 50 extension tests green.

---

### Task 1: Popup — "⚙ Settings" link to the options page

**Files:**
- Modify: `extension/src/popup/popup.html`
- Modify: `extension/src/popup/popup.ts`

Popup wiring has no unit tests in this codebase (verified via build + smoke, like the rest of `popup.ts`). This task is verified by `npm run check` (type/lint) and `npm run build`, then confirmed in the interactive smoke.

- [ ] **Step 1: Add the Settings link to `extension/src/popup/popup.html`**

Add a `#settings` style rule inside the existing `<style>` block, immediately after the `#connection` rule (line 14):

```css
      #settings { display: inline-block; margin-top: 0.5rem; font-size: 12px; color: #2668c9; cursor: pointer; text-decoration: none; }
```

Replace the connection footer line (line 30, `<div id="connection">…</div>`) with the connection line plus the link:

```html
    <div id="connection">…</div>
    <a id="settings" href="#">⚙ Settings</a>
```

- [ ] **Step 2: Wire the click handler in `extension/src/popup/popup.ts`**

Append to the end of the file (after the existing `stop` listener block, currently line 63):

```ts
el<HTMLAnchorElement>("settings").addEventListener("click", (e) => {
  // The shared-secret field lives on the options page; the popup is the
  // primary surface, so first-time pairing must be reachable from here.
  e.preventDefault();
  void browser.runtime.openOptionsPage();
});
```

- [ ] **Step 3: Verify type + lint + build**

Run (from `extension/`): `npm run check && npm run build`
Expected: 50 tests still pass (no popup unit tests added), tsc/eslint clean, `dist/firefox/src/popup/popup.html` emitted. If eslint flags the `void` floating-promise rule, the `void` prefix already satisfies it.

- [ ] **Step 4: Commit**

```bash
git add extension/src/popup/popup.html extension/src/popup/popup.ts
git commit -m "Add Settings link to popup for discoverable pairing"
```

---

### Task 2: Extension — dangling-session reconcile (clear state only after durable enqueue)

**Files:**
- Modify: `extension/src/sessions.ts`
- Modify: `extension/tests/sessions.test.ts`
- Modify: `extension/src/background.ts:163-170`

Root cause: `SessionManager.stop()` removes the `bb:session` key and *then* returns the `session_end` event for the background to enqueue. If `queue.enqueue` throws, the event is lost and the backend session stays open. Fix: `stop` takes a `commit` callback, runs it *before* removing the key, and preserves state if it throws.

- [ ] **Step 1: Rewrite the stop tests in `extension/tests/sessions.test.ts`**

Replace the existing `test("stop returns a session_end event and clears state", ...)` block (lines 46-54) with these three tests:

```ts
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
  // Session survives so a retry (or wake reconcile) can re-send session_end.
  expect((await mgr.current())?.status).toBe("recording");
});
```

Add the `EventPayload` import at the top of the file (line 2 currently imports only `SessionManager`):

```ts
import { SessionManager } from "@src/sessions";
import type { EventPayload } from "@src/types";
```

- [ ] **Step 2: Run to verify failure**

Run (from `extension/`): `npm run test -- sessions`
Expected: FAIL — `stop()` currently takes no argument and returns `{event}|null`, so `stop(async …)` calls and the `.toBe(true)/false` assertions fail to type/compile or fail at runtime.

- [ ] **Step 3: Rewrite `stop` in `extension/src/sessions.ts`**

Replace the entire `stop()` method (lines 65-72) with:

```ts
  /** Stop the active session. Durability-first: `commit` (which enqueues the
   * session_end event) runs BEFORE local state is cleared, so a throwing
   * commit leaves the session intact for a retry — otherwise a failed enqueue
   * would strand the backend session open. Returns false when none is active
   * (idempotent). Backend end_session is idempotent, so a duplicate
   * session_end after a partial failure is a harmless no-op. */
  stop(commit: (event: EventPayload) => Promise<void>): Promise<boolean> {
    return this.serialize(async () => {
      const state = await this.current();
      if (!state) return false;
      await commit({ type: "session_end", ended_at: this.now() });
      await this.local.remove(KEY);
      return true;
    });
  }
```

- [ ] **Step 4: Run to verify the session tests pass**

Run: `npm run test -- sessions`
Expected: PASS (all session tests, including the three rewritten ones).

- [ ] **Step 5: Update the caller in `extension/src/background.ts`**

Replace the `stop` case in `handleSessionCommand` (lines 163-170):

```ts
      case "stop": {
        await sessionManager.stop(async (event) => {
          await queue.enqueue(event);
          await (await ensureClient()).notifyEnqueued();
        });
        break;
      }
```

(This preserves behavior: a session_end is enqueued and the client notified; if either throws, `stop` leaves the session recording and the command's error path in the `onMessage` handler replies with current state.)

- [ ] **Step 6: Verify full gates**

Run: `npm run check`
Expected: PASS — 52 tests (50 baseline − 1 removed stop test + 3 new), tsc/eslint clean. If tsc reports an unused-var or signature error in `background.ts`, confirm the `result` variable was fully removed from the stop case.

- [ ] **Step 7: Commit**

```bash
git add extension/src/sessions.ts extension/tests/sessions.test.ts extension/src/background.ts
git commit -m "Enqueue session_end before clearing local session state"
```

---

### Task 3: Extension — EventQueue per-event storage keys

**Files:**
- Modify: `extension/src/queue.ts`
- Modify: `extension/tests/queue.test.ts`

Root cause: one `bb:queue` array holds every envelope; `enqueue`/`ack`/`drop` read-modify-write the whole array, moving the full queue byte-weight (including up-to-2 MB page-capture HTML) each time. Fix: `bb:queue:index` holds only `{seq,type}` entries; each envelope body lives under `bb:q:<seq>`, so `enqueue` writes exactly one body plus the tiny index. The public `EventQueue` API (`enqueue`/`pending`/`ack`/`drop`) is unchanged.

- [ ] **Step 1: Add layout + migration tests to `extension/tests/queue.test.ts`**

Append these tests to the end of the file:

```ts
test("enqueue stores each event under its own bb:q:<seq> key", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com")); // seq 1
  await q.enqueue(visits("https://b.com")); // seq 2
  expect(storage.data.has("bb:q:1")).toBe(true);
  expect(storage.data.has("bb:q:2")).toBe(true);
  // Legacy single-array key is not used.
  expect(storage.data.has("bb:queue")).toBe(false);
  const index = storage.data.get("bb:queue:index") as { seq: number }[];
  expect(index.map((e) => e.seq)).toEqual([1, 2]);
});

test("ack removes the acked bodies, not just the index entries", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com")); // seq 1
  await q.enqueue(visits("https://b.com")); // seq 2
  await q.ack(1);
  expect(storage.data.has("bb:q:1")).toBe(false); // body reclaimed
  expect(storage.data.has("bb:q:2")).toBe(true);
  expect((await q.pending()).map((e) => e.seq)).toEqual([2]);
});

test("migrates a legacy bb:queue array to per-event keys on first use", async () => {
  const storage = fakeStorageArea();
  await storage.set({
    "bb:seq": 2,
    "bb:queue": [
      { v: 1, seq: 1, event: visits("https://a.com") },
      { v: 1, seq: 2, event: visits("https://b.com") },
    ],
  });
  const q = new EventQueue(storage);
  expect((await q.pending()).map((e) => e.seq)).toEqual([1, 2]);
  expect(storage.data.has("bb:queue")).toBe(false); // legacy key removed
  expect(storage.data.has("bb:q:1")).toBe(true);
  // seq counter is preserved across the migration.
  expect((await q.enqueue(visits("https://c.com"))).seq).toBe(3);
});
```

Rewrite the existing cap test (`test("queue is capped: oldest events drop beyond MAX_QUEUE", ...)`) to seed the new layout instead of the legacy `bb:queue` array. Replace its body with:

```ts
test("queue is capped: oldest events drop beyond MAX_QUEUE", async () => {
  const storage = fakeStorageArea();
  // Seed a full queue directly in the per-event layout (enqueueing MAX_QUEUE
  // events one by one would be O(n^2)).
  const index = Array.from({ length: MAX_QUEUE }, (_, i) => ({
    seq: i + 1,
    type: "history_visits",
  }));
  const bodies: Record<string, unknown> = {};
  for (const e of index) {
    bodies[`bb:q:${e.seq}`] = { v: 1, seq: e.seq, event: visits(`https://p${e.seq}.com`) };
  }
  await storage.set({ "bb:seq": MAX_QUEUE, "bb:queue:index": index, ...bodies });
  const q = new EventQueue(storage);
  for (let i = 0; i < 10; i++) await q.enqueue(visits(`https://q${i}.com`)); // seq 5001..5010
  const pending = await q.pending();
  expect(pending).toHaveLength(MAX_QUEUE);
  expect(pending[0]!.seq).toBe(11); // 10 oldest dropped
});
```

- [ ] **Step 2: Run to verify failure**

Run (from `extension/`): `npm run test -- queue`
Expected: FAIL — the new `bb:q:<seq>` / `bb:queue:index` layout assertions fail (current code writes only `bb:queue`), and the migration test finds `bb:queue` still present.

- [ ] **Step 3: Rewrite `extension/src/queue.ts`**

Replace the entire file with:

```ts
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
    if (legacy && legacy.length > 0) {
      const index: IndexEntry[] = [];
      const items: Record<string, unknown> = {};
      for (const env of legacy) {
        index.push({ seq: env.seq, type: env.event.type });
        items[bodyKey(env.seq)] = env;
      }
      items[INDEX_KEY] = index;
      await this.storage.set(items);
    }
    await this.storage.remove(LEGACY_QUEUE_KEY);
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
```

- [ ] **Step 4: Run the queue tests**

Run: `npm run test -- queue`
Expected: PASS — all queue tests, including the three new layout/migration tests and the rewritten cap test.

- [ ] **Step 5: Verify full gates (no regression elsewhere)**

Run: `npm run check`
Expected: PASS — 55 tests total (52 after Task 2, +3 net from Task 3: three new layout/migration tests; the cap test is rewritten in place), tsc/eslint clean. `ws.ts` uses only the public `pending`/`ack`/`drop` API, so it is unaffected.

- [ ] **Step 6: Commit**

```bash
git add extension/src/queue.ts extension/tests/queue.test.ts
git commit -m "Store queued events under per-event storage keys"
```

---

### Task 4: Gates and smoke handoff

**Files:** none (verification only)

- [ ] **Step 1: Full extension gates**

Run (from `extension/`): `npm run check && npm run build`
Expected: 55 tests pass; tsc/eslint clean; `dist/firefox/` built. Report the `web-ext lint` state only if run — the pre-existing `data_collection_permissions` and bundled-Readability `UNSAFE_VAR_ASSIGNMENT` warnings are known and acceptable (plan `2026-07-17-session-capture-core.md` follow-ups §4).

- [ ] **Step 2: Confirm no backend impact**

This slice is extension-only; the backend protocol is unchanged (`session_end` idempotency and per-event queueing are both client-internal). No backend tests need to run, but note in the report that the change is client-only.

- [ ] **Step 3: Hand off the interactive smoke to the user** (not automatable):
  - Load `dist/firefox` via `npm run smoke`, open the popup, click **⚙ Settings** → the options page opens; enter the secret there and pair.
  - Start a named session, browse 1–2 pages, **Stop**; confirm `buddy sessions` shows the session *closed* (an `ended_at`, not "active") — the reconcile path.
  - Confirm capture still works end to end: `buddy search "<something you read>"` finds the page.
  - (Optional) With DevTools on the background page, inspect `storage.local`: queued events appear under `bb:q:<seq>` keys with a `bb:queue:index`, and no `bb:queue` array remains.

---

## Self-review notes

- **Spec coverage (Slice 0):** popup Settings link → Task 1 ✓; dangling-session reconcile via durable-enqueue-first `stop` → Task 2 ✓; EventQueue per-event keys + legacy migration → Task 3 ✓. Deferred items (REST blob endpoint, gzip-to-thread, dwell/SPA/screenshots, firewall) are explicitly out of scope in the spec and absent here by design.
- **Type consistency:** `SessionManager.stop(commit: (event: EventPayload) => Promise<void>): Promise<boolean>` is defined in Task 2 Step 3 and consumed with that exact signature in the background caller (Step 5) and the tests (Step 1). `IndexEntry {seq,type}`, `bodyKey(seq) => "bb:q:<seq>"`, `INDEX_KEY = "bb:queue:index"`, `LEGACY_QUEUE_KEY = "bb:queue"`, and `MAX_QUEUE` are all defined in Task 3 Step 3 and used consistently in the Task 3 tests.
- **Test counts:** baseline 50 → Task 2 nets +2 (remove 1 stop test, add 3) → 52 → Task 3 nets 0 to the headline count relative to its own baseline (rewrites 1 cap test in place, adds 3 layout/migration tests = +3, so 55 after Task 3). Executor: treat the exact number as informational and trust the green/red of `npm run check`.
- **Behavior preserved (queue):** monotonic seq across worker death, `tab_state` coalescing, oldest-drop cap at `MAX_QUEUE`, `ack` prunes `<= seq`, `drop` removes exactly one, mutex-serialized RMW — all exercised by the retained pre-existing tests, which use only the unchanged public API.
- **Migration safety:** `migrateIfNeeded` is idempotent and per-instance (a fresh worker re-checks the legacy key, which is already removed after first migration); it runs inside the mutex so it never re-serializes and cannot deadlock; `ack`/`drop` read the index directly rather than via the (now serialized) `pending()`, avoiding nested serialization.
