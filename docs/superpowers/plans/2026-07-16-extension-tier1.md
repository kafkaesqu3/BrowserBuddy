# BrowserBuddy Extension Tier 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working Firefox MV3 extension that continuously syncs open tabs (with active-time, container, tab-group metadata) and browser history to the local backend over the authenticated seq/ack WebSocket protocol — plus the two backend follow-ups recorded from the Milestone 1 final review.

**Architecture:** Thin, ephemeral background script wiring testable pure modules: a durable event queue (seq assignment + `storage.local` buffering + ack pruning), a tab tracker (active-time accounting in `storage.session`), a history syncer (live `onVisited` + watermark gap-fill), and a WebSocket client (first-message auth, drain-on-auth, heartbeat, backoff reconnect). All modules take injected browser-API facades so vitest tests run without a browser. A tiny build step emits per-target manifests (Firefox event page / Chrome service worker) from one base.

**Tech Stack:** TypeScript (strict), Vite, vitest, eslint (flat config, typescript-eslint), `@types/firefox-webext-browser`, `web-ext` for manual smoke. Backend fixes in the existing Python/FastAPI codebase.

**Spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**This is Plan 2 of ~6** (Milestone 2: extension Tier 1). Tier-2 session capture, popup badge states, and content scripts are Milestone 3 — do NOT build them here.

---

## File structure

```
extension/
  package.json                    # npm scripts: build, test, check
  tsconfig.json                   # strict TS, webext types
  eslint.config.js                # flat config
  vite.config.ts                  # multi-entry build (background + options page)
  manifest.base.json              # shared manifest fields
  manifest.firefox.json           # overlay: event page, gecko id, FF-only permissions
  manifest.chrome.json            # overlay: service worker, no FF-only permissions
  scripts/emit-manifests.mjs      # merge base+overlay → dist/firefox, dist/chrome
  src/
    types.ts                      # protocol v1 types (mirror backend models.py)
    settings.ts                   # backendUrl/secret/clientId in storage.local
    queue.ts                      # EventQueue: durable seq/buffer/ack/drop/coalesce
    tabs.ts                       # TabTracker: active-time accounting + snapshots
    history.ts                    # visit conversion + watermark gap-fill
    ws.ts                         # WsClient: auth, drain, ack, heartbeat, reconnect
    sync.ts                       # orchestration helpers used by background.ts
    background.ts                 # pure wiring: listeners, alarms, module glue
    options/options.html
    options/options.ts            # URL/secret form + connection status
  tests/
    fakes.ts                      # fakeStorageArea, FakeSocket, fixed clock
    settings.test.ts
    queue.test.ts
    tabs.test.ts
    history.test.ts
    ws.test.ts
    sync.test.ts

backend/  (existing — small modifications)
  src/browserbuddy/app.py         # ping/pong + per-event DB cursor check
  src/browserbuddy/importer/firefox.py  # negative visit_date filter
  src/browserbuddy/cli.py         # corrupt-DB error UX, --limit help text
  tests/test_app.py, tests/test_importer_firefox.py, tests/test_cli.py
```

Conventions: timestamps are integer ms since epoch UTC everywhere (`Date.now()`). Extension commands run from `extension/`; backend commands from `backend/`. Commit messages: imperative, no AI attribution. **Imports: never `../` (enforced by a user-level hook) — tests import sources via the `@src/*` alias (tsconfig `paths` + Vite `resolve.alias`, set up in Task 4); same-directory `./sibling` imports are fine.**

---

### Task 1: Backend — WS ping/pong + per-event cursor check (stale-connection fix)

**Files:**
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_app.py`

The Milestone 1 final review found that a stale concurrent connection (zombie MV3 worker socket) can apply an out-of-order `tab_state` because `last_seq` is cached per-connection. Fix: read the cursor from the DB per event. Also add `{"type":"ping"}` → `{"type":"pong"}` so the extension's heartbeat isn't rejected as a malformed envelope.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_app.py`:

```python
def test_ws_ping_gets_pong(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_stale_connection_cannot_apply_old_snapshot(client, app):
    # Two live sockets for the same client_id: the stale one must not
    # overwrite newer state, even though its connection-local view is old.
    with client.websocket_connect("/ws") as stale:
        _auth(stale, app, client_id="ext-1")
        with client.websocket_connect("/ws") as fresh:
            _auth(fresh, app, client_id="ext-1")
            fresh.send_json(_tab_state_envelope(2, ["https://new.com"]))
            assert fresh.receive_json() == {"type": "ack", "seq": 2}
        # Stale socket now replays seq 1: acked (so its client advances)
        # but NOT applied.
        stale.send_json(_tab_state_envelope(1, ["https://old.com"]))
        assert stale.receive_json() == {"type": "ack", "seq": 1}
    tabs = app.state.storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://new.com"]
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_app.py -v -k "ping or stale"`
Expected: `test_ws_ping_gets_pong` FAILS (receives an `error` frame, not pong); `test_ws_stale_connection_cannot_apply_old_snapshot` FAILS (old snapshot applied — tabs show `https://old.com`).

- [ ] **Step 3: Modify the WS loop in `backend/src/browserbuddy/app.py`**

Inside `ws_ingest`, replace the event-loop body so that (a) ping is answered before envelope validation, and (b) the cursor is read from the DB per event instead of the connection-local variable:

```python
        try:
            while True:
                try:
                    raw = await ws.receive_json()
                except (ValueError, KeyError):
                    # Not JSON / binary frame: unsupported data, close per RFC 6455.
                    await ws.close(code=1003)
                    return
                if raw == {"type": "ping"}:  # extension heartbeat (keeps MV3 worker alive)
                    await ws.send_json({"type": "pong"})
                    continue
                try:
                    envelope = EventEnvelope.model_validate(raw)
                except ValidationError as exc:
                    logger.warning("Rejected malformed event: %s", exc)
                    await ws.send_json({"type": "error", "detail": exc.errors()[0]["msg"]})
                    continue
                # Re-read the cursor per event: a concurrent connection for the
                # same client_id may have advanced it (stale-socket protection).
                last_seq = storage.get_last_seq(auth.client_id)
                if envelope.seq > last_seq:
                    try:
                        _apply_event(storage, envelope)
                        storage.set_last_seq(auth.client_id, envelope.seq)
                    except sqlite3.IntegrityError as exc:
                        logger.warning("Integrity error applying event: %s", exc)
                        await ws.send_json({"type": "error", "detail": "integrity error"})
                        continue
                await ws.send_json({"type": "ack", "seq": envelope.seq})
        except WebSocketDisconnect:
            pass
```

(The `last_seq` variable read after auth is still used for the `auth_ok` reply; the local caching between events is what's removed. Keep everything else — auth phase, close codes — unchanged.) Also add this one-line comment above `await ws.accept()`, recording a spec decision the final review asked us to pin in code:

```python
        # No Origin check by design: the shared secret, not the extension
        # origin, is the auth (see design spec, portability policy).
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: 51 passed (49 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py
git commit -m "Answer WS pings and re-read seq cursor per event"
```

---

### Task 2: Backend — importer corrupt-DB error UX + negative visit_date filter

**Files:**
- Modify: `backend/src/browserbuddy/importer/firefox.py`
- Modify: `backend/src/browserbuddy/cli.py`
- Modify: `backend/tests/test_importer_firefox.py`
- Modify: `backend/tests/test_cli.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_importer_firefox.py`:

```python
def test_import_skips_negative_visit_date(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    make_places(
        places,
        [
            ("https://good.com", "Good", 1720000000000000),
            ("https://bad-date.com", "Bad", -5),
        ],
    )
    assert import_places(places, storage) == 1
    assert storage.search_history("bad-date") == []
```

Append to `backend/tests/test_cli.py`:

```python
def test_import_history_corrupt_db_fails_clearly(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "places.sqlite").write_bytes(b"this is not a sqlite database")
    result = runner.invoke(
        app, ["import-history", "--profile", str(profile)], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "corrupt" in result.output.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_importer_firefox.py tests/test_cli.py -v -k "negative or corrupt"`
Expected: negative-date test FAILS (row imported — a negative µs value divides to a negative ms value which passes nothing at the SQL layer today; note the Pydantic `ge=0` would abort the batch, which is exactly the bug); corrupt-DB test FAILS (raw `sqlite3.DatabaseError` traceback, exit code != 1).

- [ ] **Step 3: Fix the importer SQL** in `backend/src/browserbuddy/importer/firefox.py` — extend the WHERE clause:

```sql
WHERE v.visit_date IS NOT NULL AND v.visit_date >= 0 AND p.url IS NOT NULL
```

- [ ] **Step 4: Fix the CLI** in `backend/src/browserbuddy/cli.py` — in `import_history`, widen the exception handling around the import call and improve the `--limit` help in `search`:

```python
    conn = db.connect(get_settings().db_path)
    try:
        count = firefox.import_places(places, Storage(conn))
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except sqlite3.DatabaseError as exc:
        typer.echo(
            f"Error: {places} is corrupt or not a SQLite database ({exc}). "
            "If Firefox is running, close it and retry.",
            err=True,
        )
        raise typer.Exit(1) from exc
    finally:
        conn.close()
```

(add `import sqlite3` to cli.py imports; keep the existing structure otherwise). In `search`, change the limit option help to `"Maximum results (capped at 200)."`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: 53 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/src/browserbuddy/importer/firefox.py backend/src/browserbuddy/cli.py \
    backend/tests/test_importer_firefox.py backend/tests/test_cli.py
git commit -m "Fail clearly on corrupt places.sqlite and skip negative visit dates"
```

---

### Task 3: Extension scaffold — npm project, configs, per-target manifests

**Files:**
- Create: `extension/package.json`, `extension/tsconfig.json`, `extension/eslint.config.js`, `extension/vite.config.ts`
- Create: `extension/manifest.base.json`, `extension/manifest.firefox.json`, `extension/manifest.chrome.json`
- Create: `extension/scripts/emit-manifests.mjs`
- Create: `extension/src/background.ts` (placeholder), `extension/src/options/options.html`, `extension/src/options/options.ts` (placeholder)
- Create: `extension/tests/smoke.test.ts`

- [ ] **Step 1: Scaffold npm project**

```bash
cd /Users/david/Projects/BrowserBuddy
mkdir -p extension/src/options extension/scripts extension/tests
cd extension
npm init -y
npm install -D typescript vite vitest eslint @eslint/js typescript-eslint \
    @types/firefox-webext-browser web-ext
```

Then edit `package.json` to set:

```json
{
  "name": "browserbuddy-extension",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "vite build && node scripts/emit-manifests.mjs",
    "test": "vitest run",
    "check": "tsc --noEmit && eslint . && vitest run",
    "smoke": "web-ext run --source-dir dist/firefox"
  }
}
```

(keep the devDependencies npm wrote; delete `main`, `keywords`, `author`, `license`, `description` stubs npm generates).

- [ ] **Step 2: Write `extension/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM"],
    "types": ["firefox-webext-browser", "vite/client"],
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src", "tests", "scripts", "vite.config.ts"]
}
```

- [ ] **Step 3: Write `extension/eslint.config.js`**

```js
import js from "@eslint/js";
import ts from "typescript-eslint";

export default ts.config(
  { ignores: ["dist/", "node_modules/"] },
  js.configs.recommended,
  ...ts.configs.recommended,
);
```

- [ ] **Step 4: Write `extension/vite.config.ts`**

```ts
import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist/assets",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        background: resolve(__dirname, "src/background.ts"),
        options: resolve(__dirname, "src/options/options.html"),
      },
      output: { entryFileNames: "[name].js" },
    },
    // MV3 background must be a plain module graph, no dynamic-import splitting.
    modulePreload: false,
    target: "es2022",
  },
  test: { environment: "node", include: ["tests/**/*.test.ts"] },
});
```

- [ ] **Step 5: Write the three manifests**

`extension/manifest.base.json`:

```json
{
  "manifest_version": 3,
  "name": "BrowserBuddy",
  "version": "0.1.0",
  "description": "Research historian: syncs tabs and history to your local BrowserBuddy backend.",
  "options_ui": { "page": "src/options/options.html" }
}
```

`extension/manifest.firefox.json` (overlay — full arrays, no merging of array contents):

```json
{
  "permissions": ["tabs", "history", "storage", "alarms", "contextualIdentities", "cookies"],
  "background": { "scripts": ["background.js"], "type": "module" },
  "browser_specific_settings": {
    "gecko": { "id": "browserbuddy@tulis.dev", "strict_min_version": "128.0" }
  }
}
```

`extension/manifest.chrome.json`:

```json
{
  "permissions": ["tabs", "history", "storage", "alarms"],
  "background": { "service_worker": "background.js", "type": "module" }
}
```

- [ ] **Step 6: Write `extension/scripts/emit-manifests.mjs`**

```js
// Merge manifest.base.json with a per-browser overlay and copy built assets
// into dist/<target>/ so each directory is a loadable extension.
import { cpSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const base = JSON.parse(readFileSync(join(root, "manifest.base.json"), "utf8"));

for (const target of ["firefox", "chrome"]) {
  const overlay = JSON.parse(readFileSync(join(root, `manifest.${target}.json`), "utf8"));
  const manifest = { ...base, ...overlay }; // top-level overlay wins; arrays replaced
  const outDir = join(root, "dist", target);
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  cpSync(join(root, "dist", "assets"), outDir, { recursive: true });
  writeFileSync(join(outDir, "manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`wrote dist/${target}/manifest.json`);
}
```

- [ ] **Step 7: Write placeholders so the build has real entries**

`extension/src/background.ts`:

```ts
// Wiring added in Task 9. Placeholder keeps the build green until then.
console.log("BrowserBuddy background loaded");
```

`extension/src/options/options.html`:

```html
<!doctype html>
<html>
  <head><meta charset="utf-8" /><title>BrowserBuddy Options</title></head>
  <body>
    <h1>BrowserBuddy</h1>
    <div id="app"></div>
    <script type="module" src="./options.ts"></script>
  </body>
</html>
```

`extension/src/options/options.ts`:

```ts
// Form logic added in Task 10.
document.querySelector("#app")!.textContent = "Options UI coming in Task 10.";
```

`extension/tests/smoke.test.ts`:

```ts
import { expect, test } from "vitest";

test("toolchain runs", () => {
  expect(1 + 1).toBe(2);
});
```

- [ ] **Step 8: Verify the toolchain**

```bash
cd /Users/david/Projects/BrowserBuddy/extension
npm run check      # tsc + eslint + vitest: expect clean, 1 test passed
npm run build      # expect dist/firefox/manifest.json and dist/chrome/manifest.json
```

Inspect `dist/firefox/manifest.json`: must contain `background.scripts`, `browser_specific_settings.gecko.id`, and the six Firefox permissions. `dist/chrome/manifest.json`: `background.service_worker`, four permissions, no `browser_specific_settings`.

- [ ] **Step 9: Commit**

```bash
cd /Users/david/Projects/BrowserBuddy
printf 'node_modules/\ndist/\n' > extension/.gitignore
git add extension
git commit -m "Scaffold extension: Vite/TS toolchain and per-target manifests"
```

---

### Task 4: Protocol types + settings module + test fakes

**Files:**
- Create: `extension/src/types.ts`, `extension/src/settings.ts`
- Create: `extension/tests/fakes.ts`, `extension/tests/settings.test.ts`

- [ ] **Step 1: Write `extension/src/types.ts`** (mirrors `backend/src/browserbuddy/models.py` exactly — field names are the wire format):

```ts
// Ingest protocol v1. Must stay in sync with backend models.py.
export const PROTOCOL_VERSION = 1;

export interface HistoryVisit {
  url: string;
  title: string;
  visit_time: number; // ms since epoch, UTC
}

export interface TabRecord {
  tab_id: number;
  window_id: number;
  url: string;
  title: string;
  group_id: number | null;
  container: string | null; // Firefox-only enrichment; null elsewhere
  opened_at: number | null;
  last_activated_at: number | null;
  active_ms: number;
}

export type EventPayload =
  | { type: "tab_state"; tabs: TabRecord[] }
  | { type: "history_visits"; visits: HistoryVisit[] };

export interface EventEnvelope {
  v: number;
  seq: number;
  event: EventPayload;
}

export type ServerFrame =
  | { type: "auth_ok"; last_seq: number }
  | { type: "ack"; seq: number }
  | { type: "error"; detail: string }
  | { type: "pong" };

// Minimal facade over browser.storage.{local,session} so modules are testable.
export interface StorageArea {
  get(keys: string | string[]): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
}
```

- [ ] **Step 2: Write `extension/tests/fakes.ts`**

```ts
import type { StorageArea } from "@src/types";

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
```

- [ ] **Step 3: Write failing tests** — `extension/tests/settings.test.ts`:

```ts
import { expect, test } from "vitest";
import { DEFAULT_BACKEND_URL, loadSettings, saveSettings } from "@src/settings";
import { fakeStorageArea } from "./fakes";

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
```

- [ ] **Step 4: Run to verify failure**

Run: `npm test`
Expected: FAIL — cannot resolve `../src/settings`.

- [ ] **Step 5: Write `extension/src/settings.ts`**

```ts
import type { StorageArea } from "./types";

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
```

- [ ] **Step 6: Run tests**

Run: `npm test` — expected: 3 passed (smoke + 2 new). Also `npm run check` clean.

- [ ] **Step 7: Commit**

```bash
git add extension/src/types.ts extension/src/settings.ts extension/tests
git commit -m "Add protocol types, settings module, and test fakes"
```

---

### Task 5: Durable event queue (seq, buffer, ack, drop, coalesce)

**Files:**
- Create: `extension/src/queue.ts`
- Create: `extension/tests/queue.test.ts`

- [ ] **Step 1: Write failing tests** — `extension/tests/queue.test.ts`:

```ts
import { expect, test } from "vitest";
import { EventQueue, MAX_QUEUE } from "@src/queue";
import type { EventPayload } from "@src/types";
import { fakeStorageArea } from "./fakes";

const tabState = (url: string): EventPayload => ({
  type: "tab_state",
  tabs: [{
    tab_id: 1, window_id: 1, url, title: "", group_id: null,
    container: null, opened_at: null, last_activated_at: null, active_ms: 0,
  }],
});
const visits = (url: string): EventPayload => ({
  type: "history_visits",
  visits: [{ url, title: "", visit_time: 1720000000000 }],
});

test("enqueue assigns monotonically increasing seq and persists", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  const a = await q.enqueue(visits("https://a.com"));
  const b = await q.enqueue(visits("https://b.com"));
  expect([a.seq, b.seq]).toEqual([1, 2]);
  expect(a.v).toBe(1);
  // A fresh queue over the same storage sees the same state (worker death).
  const q2 = new EventQueue(storage);
  expect((await q2.pending()).map((e) => e.seq)).toEqual([1, 2]);
  expect((await q2.enqueue(visits("https://c.com"))).seq).toBe(3);
});

test("ack prunes everything at or below the acked seq", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com"));
  await q.enqueue(visits("https://b.com"));
  await q.enqueue(visits("https://c.com"));
  await q.ack(2);
  expect((await q.pending()).map((e) => e.seq)).toEqual([3]);
});

test("drop removes exactly one seq (server error frame = skip)", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(visits("https://a.com"));
  await q.enqueue(visits("https://b.com"));
  await q.drop(1);
  expect((await q.pending()).map((e) => e.seq)).toEqual([2]);
});

test("enqueue coalesces pending tab_state (only latest snapshot matters)", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  await q.enqueue(tabState("https://old.com"));
  await q.enqueue(visits("https://v.com"));
  const latest = await q.enqueue(tabState("https://new.com"));
  const pending = await q.pending();
  const tabEvents = pending.filter((e) => e.event.type === "tab_state");
  expect(tabEvents).toHaveLength(1);
  expect(tabEvents[0]!.seq).toBe(latest.seq);
  // history_visits are never coalesced
  expect(pending.some((e) => e.event.type === "history_visits")).toBe(true);
});

test("queue is capped: oldest events drop beyond MAX_QUEUE", async () => {
  const storage = fakeStorageArea();
  const q = new EventQueue(storage);
  for (let i = 0; i < MAX_QUEUE + 10; i++) await q.enqueue(visits(`https://p${i}.com`));
  const pending = await q.pending();
  expect(pending).toHaveLength(MAX_QUEUE);
  expect(pending[0]!.seq).toBe(11); // 10 oldest dropped
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm test` — expected: FAIL, cannot resolve `../src/queue`.

- [ ] **Step 3: Write `extension/src/queue.ts`**

```ts
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
```

- [ ] **Step 4: Run tests**

Run: `npm test` — expected: 8 passed. `npm run check` clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/queue.ts extension/tests/queue.test.ts
git commit -m "Add durable event queue with seq, ack, drop, and coalescing"
```

---

### Task 6: Tab tracker — active-time accounting + snapshots

**Files:**
- Create: `extension/src/tabs.ts`
- Create: `extension/tests/tabs.test.ts`

- [ ] **Step 1: Write failing tests** — `extension/tests/tabs.test.ts`:

```ts
import { expect, test } from "vitest";
import { TabTracker, type BrowserTab } from "@src/tabs";
import { fakeStorageArea } from "./fakes";

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

const tab = (id: number, url: string, extra: Partial<BrowserTab> = {}): BrowserTab => ({
  id, windowId: 1, url, title: `t${id}`, ...extra,
});

const noContainer = async () => null;

test("active time accumulates for the focused tab only", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(1, 1);
  c.advance(5000);
  await tracker.recordActivation(2, 1); // switch away: tab 1 banked 5s
  c.advance(3000);
  const records = await tracker.snapshot([tab(1, "https://a.com"), tab(2, "https://b.com")], noContainer);
  const byId = new Map(records.map((r) => [r.tab_id, r]));
  expect(byId.get(1)!.active_ms).toBe(5000);
  expect(byId.get(2)!.active_ms).toBe(3000); // still active: counted up to now
  expect(byId.get(1)!.last_activated_at).toBe(1_000_000);
  expect(byId.get(2)!.last_activated_at).toBe(1_005_000);
});

test("focus lost stops the clock", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(1, 1);
  c.advance(2000);
  await tracker.recordFocusLost();
  c.advance(60_000); // user is away: must not count
  const records = await tracker.snapshot([tab(1, "https://a.com")], noContainer);
  expect(records[0]!.active_ms).toBe(2000);
});

test("state survives tracker re-creation (worker death)", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const t1 = new TabTracker(session, c.now);
  await t1.recordActivation(1, 1);
  c.advance(4000);
  const t2 = new TabTracker(session, c.now); // fresh instance, same storage
  await t2.recordActivation(2, 1);
  const records = await t2.snapshot([tab(1, "https://a.com"), tab(2, "https://b.com")], noContainer);
  expect(records.find((r) => r.tab_id === 1)!.active_ms).toBe(4000);
});

test("snapshot prunes state for closed tabs and fills metadata", async () => {
  const c = clock();
  const session = fakeStorageArea();
  const tracker = new TabTracker(session, c.now);
  await tracker.recordActivation(99, 1); // tab that will be gone
  const records = await tracker.snapshot(
    [tab(1, "https://a.com", { groupId: 7, cookieStoreId: "firefox-container-1" })],
    async (storeId) => (storeId === "firefox-container-1" ? "Research" : null),
  );
  expect(records).toHaveLength(1);
  expect(records[0]!.group_id).toBe(7);
  expect(records[0]!.container).toBe("Research");
  expect(records[0]!.opened_at).toBe(1_000_000); // first-seen fallback
  // pruned: tab 99 no longer in stored accounting
  const stored = await session.get("bb:tabstate");
  expect(JSON.stringify(stored)).not.toContain("99");
});

test("tabs without id or url are skipped; groupId -1 maps to null", async () => {
  const c = clock();
  const tracker = new TabTracker(fakeStorageArea(), c.now);
  const records = await tracker.snapshot(
    [tab(1, "https://a.com", { groupId: -1 }), { windowId: 1, title: "no id" }, tab(2, "")],
    noContainer,
  );
  expect(records).toHaveLength(1);
  expect(records[0]!.group_id).toBeNull();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm test` — expected: FAIL, cannot resolve `../src/tabs`.

- [ ] **Step 3: Write `extension/src/tabs.ts`**

```ts
import type { StorageArea, TabRecord } from "./types";

/** The subset of browser.tabs.Tab we consume (keeps tests browser-free). */
export interface BrowserTab {
  id?: number;
  windowId?: number;
  url?: string;
  title?: string;
  groupId?: number;
  cookieStoreId?: string;
}

interface PerTab {
  opened_at: number;
  last_activated_at: number | null;
  active_ms: number;
}

interface TrackerState {
  activeTabId: number | null;
  activeSince: number | null;
  perTab: Record<string, PerTab>;
}

const STATE_KEY = "bb:tabstate";
const EMPTY: TrackerState = { activeTabId: null, activeSince: null, perTab: {} };

/** Active-time accounting. State lives in storage.session so it survives
 * background-worker suspension but resets with the browser (like tabs do). */
export class TabTracker {
  constructor(
    private session: StorageArea,
    private now: () => number,
  ) {}

  private async load(): Promise<TrackerState> {
    const stored = (await this.session.get(STATE_KEY))[STATE_KEY] as TrackerState | undefined;
    return stored ?? structuredClone(EMPTY);
  }

  private async store(state: TrackerState): Promise<void> {
    await this.session.set({ [STATE_KEY]: state });
  }

  private ensure(state: TrackerState, tabId: number): PerTab {
    const key = String(tabId);
    state.perTab[key] ??= { opened_at: this.now(), last_activated_at: null, active_ms: 0 };
    return state.perTab[key];
  }

  /** Bank active time for the previously active tab. */
  private bank(state: TrackerState): void {
    if (state.activeTabId === null || state.activeSince === null) return;
    this.ensure(state, state.activeTabId).active_ms += this.now() - state.activeSince;
    state.activeTabId = null;
    state.activeSince = null;
  }

  async recordActivation(tabId: number, _windowId: number): Promise<void> {
    const state = await this.load();
    this.bank(state);
    const entry = this.ensure(state, tabId);
    entry.last_activated_at = this.now();
    state.activeTabId = tabId;
    state.activeSince = this.now();
    await this.store(state);
  }

  async recordFocusLost(): Promise<void> {
    const state = await this.load();
    this.bank(state);
    await this.store(state);
  }

  /** Build TabRecords for all live tabs; prune accounting for closed ones. */
  async snapshot(
    tabs: BrowserTab[],
    containerName: (cookieStoreId?: string) => Promise<string | null>,
  ): Promise<TabRecord[]> {
    const state = await this.load();
    const records: TabRecord[] = [];
    const liveKeys = new Set<string>();
    for (const tab of tabs) {
      if (tab.id === undefined || tab.windowId === undefined || !tab.url) continue;
      liveKeys.add(String(tab.id));
      const entry = this.ensure(state, tab.id);
      let activeMs = entry.active_ms;
      if (state.activeTabId === tab.id && state.activeSince !== null) {
        activeMs += this.now() - state.activeSince; // include the open interval
      }
      records.push({
        tab_id: tab.id,
        window_id: tab.windowId,
        url: tab.url,
        title: tab.title ?? "",
        group_id: tab.groupId === undefined || tab.groupId === -1 ? null : tab.groupId,
        container: await containerName(tab.cookieStoreId),
        opened_at: entry.opened_at,
        last_activated_at: entry.last_activated_at,
        active_ms: activeMs,
      });
    }
    for (const key of Object.keys(state.perTab)) {
      if (!liveKeys.has(key)) delete state.perTab[key];
    }
    await this.store(state);
    return records;
  }
}
```

- [ ] **Step 4: Run tests**

Run: `npm test` — expected: 13 passed. `npm run check` clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/tabs.ts extension/tests/tabs.test.ts
git commit -m "Add tab tracker with session-persisted active-time accounting"
```

---

### Task 7: History sync — visit conversion + watermark gap-fill

**Files:**
- Create: `extension/src/history.ts`
- Create: `extension/tests/history.test.ts`

- [ ] **Step 1: Write failing tests** — `extension/tests/history.test.ts`:

```ts
import { expect, test } from "vitest";
import { gapFillVisits, visitFromHistoryItem } from "@src/history";
import { fakeStorageArea } from "./fakes";

test("visitFromHistoryItem converts and rejects unusable items", () => {
  expect(
    visitFromHistoryItem({ url: "https://a.com", title: "A", lastVisitTime: 1720000000000.7 }),
  ).toEqual({ url: "https://a.com", title: "A", visit_time: 1720000000000 });
  expect(visitFromHistoryItem({ url: "https://a.com" })).toBeNull(); // no time
  expect(visitFromHistoryItem({ lastVisitTime: 1 })).toBeNull(); // no url
  expect(visitFromHistoryItem({ url: "https://a.com", lastVisitTime: 1 })!.title).toBe("");
});

test("gapFillVisits searches since watermark and advances it", async () => {
  const storage = fakeStorageArea();
  await storage.set({ "bb:history_watermark": 500 });
  const calls: unknown[] = [];
  const historyApi = {
    async search(q: { text: string; startTime: number; maxResults: number }) {
      calls.push(q);
      return [
        { url: "https://a.com", title: "A", lastVisitTime: 600 },
        { url: "https://skipme.com" }, // no lastVisitTime → skipped
      ];
    },
  };
  const visits = await gapFillVisits(historyApi, storage, () => 900);
  expect(visits).toEqual([{ url: "https://a.com", title: "A", visit_time: 600 }]);
  expect(calls[0]).toMatchObject({ text: "", startTime: 500 });
  expect((await storage.get("bb:history_watermark"))["bb:history_watermark"]).toBe(900);
});

test("first run uses now as watermark start (no historical flood)", async () => {
  const storage = fakeStorageArea();
  const historyApi = { async search() { return []; } };
  await gapFillVisits(historyApi, storage, () => 12345);
  expect((await storage.get("bb:history_watermark"))["bb:history_watermark"]).toBe(12345);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm test` — expected: FAIL, cannot resolve `../src/history`.

- [ ] **Step 3: Write `extension/src/history.ts`**

```ts
import type { HistoryVisit, StorageArea } from "./types";

const WATERMARK_KEY = "bb:history_watermark";

export interface HistoryItemLike {
  url?: string;
  title?: string;
  lastVisitTime?: number;
}

export interface HistoryApi {
  search(query: { text: string; startTime: number; maxResults: number }): Promise<HistoryItemLike[]>;
}

export function visitFromHistoryItem(item: HistoryItemLike): HistoryVisit | null {
  if (!item.url || item.lastVisitTime === undefined) return null;
  return { url: item.url, title: item.title ?? "", visit_time: Math.floor(item.lastVisitTime) };
}

/** Catch up on visits recorded while the extension was asleep or disconnected.
 * Live visits arrive via history.onVisited; this covers the gaps. The backend
 * dedupes on (url, visit_time), so overlap is harmless. The one-time deep
 * backfill is `buddy import-history` (places.sqlite), not this. */
export async function gapFillVisits(
  historyApi: HistoryApi,
  storage: StorageArea,
  now: () => number = Date.now,
): Promise<HistoryVisit[]> {
  const stored = (await storage.get(WATERMARK_KEY))[WATERMARK_KEY] as number | undefined;
  const startTime = stored ?? now();
  const items = stored === undefined
    ? []
    : await historyApi.search({ text: "", startTime, maxResults: 5000 });
  const visits = items
    .map(visitFromHistoryItem)
    .filter((v): v is HistoryVisit => v !== null);
  await storage.set({ [WATERMARK_KEY]: now() });
  return visits;
}
```

- [ ] **Step 4: Run tests**

Run: `npm test` — expected: 16 passed. `npm run check` clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/history.ts extension/tests/history.test.ts
git commit -m "Add history visit conversion and watermark gap-fill"
```

---

### Task 8: WebSocket client — auth, drain, FIFO ack/error, heartbeat, reconnect

**Files:**
- Create: `extension/src/ws.ts`
- Modify: `extension/tests/fakes.ts` (add FakeSocket)
- Create: `extension/tests/ws.test.ts`

Protocol facts this client relies on (all true of the backend as of Task 1): the server replies to every client message **in order** — `ack` or `error` per envelope, `pong` per ping — so a FIFO in-flight list gives exact correspondence; `auth_ok.last_seq` tells us what the server already has; an `error` frame means "skip this event, never blind-retry".

- [ ] **Step 1: Add FakeSocket to `extension/tests/fakes.ts`** (append):

```ts
import type { WsLike } from "@src/ws";

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
```

- [ ] **Step 2: Write failing tests** — `extension/tests/ws.test.ts`:

```ts
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
  await vi.runOnlyPendingTimersAsync();
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
  await vi.runOnlyPendingTimersAsync(); // both sent, FIFO inflight [1, 2]
  s.receive({ type: "error", detail: "bad" }); // corresponds to seq 1
  s.receive({ type: "ack", seq: 2 });
  await vi.runOnlyPendingTimersAsync();
  expect(await queue.pending()).toEqual([]); // 1 dropped, 2 acked
});

test("events enqueued while authed are pushed immediately", async () => {
  const { sockets, queue, client } = setup();
  client.connect();
  const s = sockets[0]!;
  s.open();
  s.receive({ type: "auth_ok", last_seq: 0 });
  await vi.runOnlyPendingTimersAsync();
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
  await vi.runOnlyPendingTimersAsync();
  await vi.advanceTimersByTimeAsync(20_000); // heartbeat fires
  expect(s.sentJson()).toContainEqual({ type: "ping" });
  await queue.enqueue(visits("https://a.com")); // seq 1
  await client.notifyEnqueued();
  s.receive({ type: "pong" }); // pops the ping marker, not the envelope
  s.receive({ type: "error", detail: "bad" }); // must drop seq 1, not a ping
  await vi.runOnlyPendingTimersAsync();
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
  await vi.runOnlyPendingTimersAsync(); // backoff reset
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
```

- [ ] **Step 3: Run to verify failure**

Run: `npm test` — expected: FAIL, cannot resolve `../src/ws`.

- [ ] **Step 4: Write `extension/src/ws.ts`**

```ts
import type { EventQueue } from "./queue";
import type { ServerFrame } from "./types";

export type WsStatus = "disconnected" | "connecting" | "authed";

/** The subset of WebSocket we use (injectable for tests). */
export interface WsLike {
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: (() => void) | null;
}

export interface WsClientOptions {
  url: string;
  secret: string;
  clientId: string;
  queue: EventQueue;
  makeSocket: (url: string) => WsLike;
  heartbeatMs?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  onStatus?: (status: WsStatus) => void;
}

export class WsClient {
  status: WsStatus = "disconnected";

  private socket: WsLike | null = null;
  // FIFO of what we've sent and not yet had answered. The server replies
  // strictly in order (ack/error per envelope, pong per ping), so the head
  // of this list is always what the next reply refers to.
  private inflight: (number | "ping")[] = [];
  private sentUpTo = 0;
  private attempts = 0;
  private stopped = false;
  private heartbeat: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private opts: WsClientOptions) {}

  connect(): void {
    if (this.socket || this.stopped) return;
    this.setStatus("connecting");
    const socket = this.opts.makeSocket(this.opts.url);
    this.socket = socket;
    socket.onopen = () => {
      socket.send(
        JSON.stringify({ type: "auth", secret: this.opts.secret, client_id: this.opts.clientId }),
      );
    };
    socket.onmessage = (ev) => void this.handleFrame(JSON.parse(ev.data) as ServerFrame);
    socket.onclose = () => this.handleClose();
  }

  /** Stop for good (settings changed / extension shutdown). */
  disconnect(): void {
    this.stopped = true;
    clearTimeout(this.reconnectTimer);
    this.teardownSocket();
    this.setStatus("disconnected");
  }

  async notifyEnqueued(): Promise<void> {
    if (this.status === "authed") await this.drain();
  }

  private async handleFrame(frame: ServerFrame): Promise<void> {
    switch (frame.type) {
      case "auth_ok": {
        this.attempts = 0;
        this.inflight = [];
        this.sentUpTo = frame.last_seq;
        await this.opts.queue.ack(frame.last_seq);
        this.setStatus("authed");
        this.startHeartbeat();
        await this.drain();
        break;
      }
      case "ack": {
        this.inflight.shift();
        await this.opts.queue.ack(frame.seq);
        break;
      }
      case "error": {
        const head = this.inflight.shift();
        console.warn("BrowserBuddy: server rejected event", head, frame.detail);
        if (typeof head === "number") await this.opts.queue.drop(head);
        break;
      }
      case "pong": {
        this.inflight.shift();
        break;
      }
    }
  }

  private async drain(): Promise<void> {
    const socket = this.socket;
    if (!socket || this.status !== "authed") return;
    for (const envelope of await this.opts.queue.pending()) {
      if (envelope.seq <= this.sentUpTo) continue;
      socket.send(JSON.stringify(envelope));
      this.inflight.push(envelope.seq);
      this.sentUpTo = envelope.seq;
    }
  }

  private startHeartbeat(): void {
    clearInterval(this.heartbeat);
    // setInterval, not alarms: sub-30s periods aren't allowed for alarms, and
    // the interval only needs to live as long as the socket does — WS traffic
    // is itself what keeps the MV3 worker alive.
    this.heartbeat = setInterval(() => {
      this.socket?.send(JSON.stringify({ type: "ping" }));
      this.inflight.push("ping");
    }, this.opts.heartbeatMs ?? 20_000);
  }

  private handleClose(): void {
    this.teardownSocket();
    this.setStatus("disconnected");
    if (this.stopped) return;
    const base = this.opts.reconnectBaseMs ?? 1000;
    const max = this.opts.reconnectMaxMs ?? 60_000;
    const delay = Math.min(base * 2 ** this.attempts, max);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private teardownSocket(): void {
    clearInterval(this.heartbeat);
    if (this.socket) {
      this.socket.onclose = null; // avoid re-entrant handleClose from close()
      this.socket.close();
      this.socket = null;
    }
    this.inflight = [];
  }

  private setStatus(status: WsStatus): void {
    this.status = status;
    this.opts.onStatus?.(status);
  }
}
```

- [ ] **Step 5: Run tests**

Run: `npm test` — expected: 22 passed. `npm run check` clean.

- [ ] **Step 6: Commit**

```bash
git add extension/src/ws.ts extension/tests/ws.test.ts extension/tests/fakes.ts
git commit -m "Add WebSocket client with auth, drain, heartbeat, and backoff"
```

---

### Task 9: Sync orchestration + background wiring

**Files:**
- Create: `extension/src/sync.ts`
- Create: `extension/tests/sync.test.ts`
- Modify: `extension/src/background.ts` (replace placeholder)

- [ ] **Step 1: Write failing tests** — `extension/tests/sync.test.ts`:

```ts
import { expect, test } from "vitest";
import { EventQueue } from "@src/queue";
import { makeContainerResolver, pushTabSnapshot, pushVisit } from "@src/sync";
import { TabTracker } from "@src/tabs";
import { fakeStorageArea } from "./fakes";

function deps() {
  const queue = new EventQueue(fakeStorageArea());
  const tracker = new TabTracker(fakeStorageArea(), () => 1_000_000);
  const notified: number[] = [];
  return {
    queue,
    tracker,
    notified,
    client: { notifyEnqueued: async () => void notified.push(1) },
    containerName: async () => null,
  };
}

test("pushTabSnapshot enqueues a tab_state and notifies the client", async () => {
  const d = deps();
  await pushTabSnapshot({
    ...d,
    tabsQuery: async () => [{ id: 1, windowId: 1, url: "https://a.com", title: "A" }],
  });
  const pending = await d.queue.pending();
  expect(pending).toHaveLength(1);
  expect(pending[0]!.event.type).toBe("tab_state");
  expect(d.notified).toHaveLength(1);
});

test("pushVisit enqueues a history_visits event and skips unusable items", async () => {
  const d = deps();
  await pushVisit(d, { url: "https://a.com", title: "A", lastVisitTime: 1720000000000 });
  await pushVisit(d, { url: "https://no-time.com" }); // skipped
  const pending = await d.queue.pending();
  expect(pending).toHaveLength(1);
  expect(pending[0]!.event).toEqual({
    type: "history_visits",
    visits: [{ url: "https://a.com", title: "A", visit_time: 1720000000000 }],
  });
});

test("makeContainerResolver: names containers, nulls defaults, tolerates absence", async () => {
  const withApi = makeContainerResolver({
    get: async (id: string) => ({ name: id === "firefox-container-1" ? "Research" : "Other" }),
  });
  expect(await withApi("firefox-container-1")).toBe("Research");
  expect(await withApi("firefox-default")).toBeNull();
  expect(await withApi(undefined)).toBeNull();

  const noApi = makeContainerResolver(undefined); // Chrome
  expect(await noApi("anything")).toBeNull();

  const broken = makeContainerResolver({
    get: async () => {
      throw new Error("no such container");
    },
  });
  expect(await broken("firefox-container-9")).toBeNull();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm test` — expected: FAIL, cannot resolve `../src/sync`.

- [ ] **Step 3: Write `extension/src/sync.ts`**

```ts
import { visitFromHistoryItem, type HistoryItemLike } from "./history";
import type { EventQueue } from "./queue";
import type { BrowserTab, TabTracker } from "./tabs";

export interface SyncDeps {
  queue: EventQueue;
  tracker: TabTracker;
  client: { notifyEnqueued(): Promise<void> };
  containerName: (cookieStoreId?: string) => Promise<string | null>;
}

export async function pushTabSnapshot(
  deps: SyncDeps & { tabsQuery: () => Promise<BrowserTab[]> },
): Promise<void> {
  const tabs = await deps.tabsQuery();
  const records = await deps.tracker.snapshot(tabs, deps.containerName);
  await deps.queue.enqueue({ type: "tab_state", tabs: records });
  await deps.client.notifyEnqueued();
}

export async function pushVisit(deps: SyncDeps, item: HistoryItemLike): Promise<void> {
  const visit = visitFromHistoryItem(item);
  if (!visit) return;
  await deps.queue.enqueue({ type: "history_visits", visits: [visit] });
  await deps.client.notifyEnqueued();
}

interface ContextualIdentitiesLike {
  get(cookieStoreId: string): Promise<{ name: string }>;
}

/** Firefox enhancement layer: resolve a tab's container name, or null on
 * Chrome / default stores / lookup failure. Results are cached per store id. */
export function makeContainerResolver(
  api: ContextualIdentitiesLike | undefined,
): (cookieStoreId?: string) => Promise<string | null> {
  const cache = new Map<string, string | null>();
  return async (cookieStoreId) => {
    if (!api || !cookieStoreId || !cookieStoreId.startsWith("firefox-container-")) return null;
    if (cache.has(cookieStoreId)) return cache.get(cookieStoreId)!;
    let name: string | null = null;
    try {
      name = (await api.get(cookieStoreId)).name;
    } catch {
      name = null;
    }
    cache.set(cookieStoreId, name);
    return name;
  };
}
```

- [ ] **Step 4: Run tests, then replace `extension/src/background.ts`**

Run: `npm test` — expected: 27 passed. Then write the wiring (not unit-tested — covered by the Task 11 manual smoke; all listeners are registered at the top level as MV3 requires):

```ts
import { gapFillVisits } from "./history";
import { EventQueue } from "./queue";
import { loadSettings } from "./settings";
import { makeContainerResolver, pushTabSnapshot, pushVisit } from "./sync";
import { TabTracker } from "./tabs";
import type { StorageArea } from "./types";
import { WsClient, type WsLike } from "./ws";

const local = browser.storage.local as unknown as StorageArea;
const session = browser.storage.session as unknown as StorageArea;

const queue = new EventQueue(local);
const tracker = new TabTracker(session, Date.now);
const containerName = makeContainerResolver(
  // Firefox-only API; undefined on Chrome (enhancement layer).
  (globalThis as { browser?: { contextualIdentities?: never } }).browser?.contextualIdentities,
);

let client: WsClient | null = null;

async function ensureClient(): Promise<WsClient> {
  if (client) return client;
  const settings = await loadSettings(local);
  client = new WsClient({
    url: settings.backendUrl,
    secret: settings.secret,
    clientId: settings.clientId,
    queue,
    makeSocket: (url) => new WebSocket(url) as unknown as WsLike,
  });
  client.connect();
  return client;
}

const deps = async () => ({
  queue,
  tracker,
  client: await ensureClient(),
  containerName,
  tabsQuery: () => browser.tabs.query({}) as Promise<import("./tabs").BrowserTab[]>,
});

let snapshotTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleSnapshot(): void {
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(() => {
    void deps().then(pushTabSnapshot);
  }, 2000);
}

browser.tabs.onActivated.addListener((info) => {
  void tracker.recordActivation(info.tabId, info.windowId).then(scheduleSnapshot);
});
browser.tabs.onCreated.addListener(() => scheduleSnapshot());
browser.tabs.onRemoved.addListener(() => scheduleSnapshot());
browser.tabs.onUpdated.addListener((_id, changeInfo) => {
  if (changeInfo.url || changeInfo.title) scheduleSnapshot();
});
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    void tracker.recordFocusLost().then(scheduleSnapshot);
  } else {
    void browser.tabs.query({ active: true, windowId }).then(([tab]) => {
      if (tab?.id !== undefined) {
        void tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
      }
    });
  }
});

browser.history.onVisited.addListener((item) => {
  void deps().then((d) => pushVisit(d, item));
});

// Periodic safety net: refresh active_ms, reconnect if needed, fill gaps.
browser.alarms.create("bb-sync", { periodInMinutes: 1 });
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "bb-sync") return;
  void (async () => {
    const d = await deps();
    const visits = await gapFillVisits(browser.history, local);
    for (const v of visits) {
      await queue.enqueue({ type: "history_visits", visits: [v] });
    }
    await d.client.notifyEnqueued();
    await pushTabSnapshot(d);
  })();
});

// Options page asks for connection status.
browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message === "bb:status") {
    sendResponse({ status: client?.status ?? "disconnected" });
  }
});

// Settings changed: rebuild the client with the new URL/secret.
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["bb:settings"]) {
    client?.disconnect();
    client = null;
    void ensureClient();
  }
});

void ensureClient();
```

Run `npm run check` and fix any type errors the wiring surfaces (the browser typings sometimes need small casts at the facade boundaries — casts belong here in background.ts, never inside the tested modules).

- [ ] **Step 5: Commit**

```bash
git add extension/src/sync.ts extension/tests/sync.test.ts extension/src/background.ts
git commit -m "Add sync orchestration and background wiring"
```

---

### Task 10: Options page — pairing UI

**Files:**
- Modify: `extension/src/options/options.html`, `extension/src/options/options.ts`

- [ ] **Step 1: Replace `extension/src/options/options.html`**

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>BrowserBuddy Options</title>
    <style>
      body { font: 14px system-ui; max-width: 480px; margin: 2rem auto; }
      label { display: block; margin-top: 1rem; font-weight: 600; }
      input { width: 100%; padding: 0.4rem; box-sizing: border-box; }
      button { margin-top: 1rem; padding: 0.4rem 1.2rem; }
      #status { margin-top: 1rem; }
      .authed { color: green; }
      .disconnected { color: crimson; }
    </style>
  </head>
  <body>
    <h1>BrowserBuddy</h1>
    <label for="url">Backend WebSocket URL</label>
    <input id="url" type="text" placeholder="ws://127.0.0.1:8765/ws" />
    <label for="secret">Shared secret (run <code>buddy secret</code>)</label>
    <input id="secret" type="password" />
    <button id="save">Save</button>
    <p id="status">Status: …</p>
    <script type="module" src="./options.ts"></script>
  </body>
</html>
```

- [ ] **Step 2: Replace `extension/src/options/options.ts`**

```ts
import { loadSettings, saveSettings } from "../settings";
import type { StorageArea } from "../types";

const local = browser.storage.local as unknown as StorageArea;
const urlInput = document.querySelector<HTMLInputElement>("#url")!;
const secretInput = document.querySelector<HTMLInputElement>("#secret")!;
const statusEl = document.querySelector<HTMLElement>("#status")!;

async function init(): Promise<void> {
  const settings = await loadSettings(local);
  urlInput.value = settings.backendUrl;
  secretInput.value = settings.secret;
}

document.querySelector("#save")!.addEventListener("click", () => {
  void saveSettings(local, {
    backendUrl: urlInput.value.trim(),
    secret: secretInput.value.trim(),
  });
});

async function refreshStatus(): Promise<void> {
  try {
    const reply = (await browser.runtime.sendMessage("bb:status")) as { status: string };
    statusEl.textContent = `Status: ${reply.status}`;
    statusEl.className = reply.status;
  } catch {
    statusEl.textContent = "Status: background unavailable";
  }
}

void init();
void refreshStatus();
setInterval(() => void refreshStatus(), 2000);
```

- [ ] **Step 3: Verify**

Run: `npm run check && npm run build` — expected: clean; `dist/firefox/` contains the options page assets.

- [ ] **Step 4: Commit**

```bash
git add extension/src/options
git commit -m "Add options page for backend URL and secret pairing"
```

---

### Task 11: Build, end-to-end smoke, gates, README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Full gates, both projects**

```bash
cd /Users/david/Projects/BrowserBuddy/extension && npm run check && npm run build
cd /Users/david/Projects/BrowserBuddy/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: extension clean (27 tests), backend clean (53 tests).

- [ ] **Step 2: End-to-end smoke (manual, real Firefox)**

```bash
# Terminal 1
cd /Users/david/Projects/BrowserBuddy/backend && uv run buddy serve
# Terminal 2
cd /Users/david/Projects/BrowserBuddy/backend && uv run buddy secret   # copy output
cd /Users/david/Projects/BrowserBuddy/extension && npm run smoke       # launches Firefox with the extension
```

In the launched Firefox: open the extension's options (about:addons → BrowserBuddy → Preferences), paste the secret, Save. Wait ~5s, browse to two or three pages, then verify from Terminal 2:

```bash
uv run buddy tabs     # expect the launched Firefox's tabs, with active-time populated
uv run buddy search "<a word from a page title you just visited>"   # expect a hit (source=sync)
```

Also verify resilience: Ctrl-C the server, browse two more pages, restart `buddy serve`, wait ~10s (reconnect backoff), and confirm `buddy tabs` reflects the latest state and the visited pages appear in search — this proves buffering + seq/ack + reconnect end to end. Report actual results honestly; if anything fails, debug root-cause before proceeding.

- [ ] **Step 3: Update `README.md`** — append after the backend quick-start section:

```markdown
## Extension (Firefox) quick start

```bash
cd extension
npm install
npm run build          # emits dist/firefox and dist/chrome
npm run smoke          # web-ext: launches Firefox with the extension loaded
```

Pair it: run `buddy secret`, paste into the extension's options page, Save.
The extension then continuously syncs open tabs and history to the backend
(`buddy tabs`, `buddy search ...`). Offline-safe: events buffer in extension
storage and replay on reconnect.
```

- [ ] **Step 4: Commit**

```bash
cd /Users/david/Projects/BrowserBuddy
git add README.md
git commit -m "Document extension quick start"
```

---

## Follow-ups for Milestone 3 (session capture)

Recorded from the final whole-branch review (2026-07-17); none block this
milestone:

1. **Auth-failure UX:** `WsLike.onclose` carries no close code, so a 4401
   (wrong secret) is indistinguishable from backend-down — infinite silent
   backoff; options page shows only "disconnected". Surface the close code
   before the interface calcifies.
2. **First-save clientId race:** options page and background can both
   generate a clientId concurrently on a fresh install; converges via the
   onChanged rebuild, but deterministic first-write would be cleaner.
3. **AMO `data_collection_permissions`:** the one web-ext lint warning —
   becomes a hard AMO requirement; needs `strict_min_version >= 140` to
   declare, so revisit when bumping the Firefox floor. (Zero-warnings
   justification: unfixable at our current min version; submission-time
   concern only.)
4. **Chrome options page:** `options.ts` lacks the `globalThis.browser ??=
   chrome` alias background.ts has — the emitted dist/chrome options page
   throws ReferenceError. Part of the deferred Chrome port.
5. Minor code hardenings for early M3: `Math.max(0, …)` clamp on active_ms
   banking (wall-clock regression can poison tab_state via the backend's
   ge=0 validation — do this one first); null `onmessage`/`onopen` in
   teardownSocket; `logged()` on the onFocusChanged/alarms.get chains;
   idle-API integration so focused-but-away time doesn't count as active;
   EventQueue full-array rewrite per enqueue may bottleneck at M3 event
   rates (consider chunked storage keys).

## Self-review notes

- **Spec coverage (this milestone):** Tier-1 ambient awareness ✓ (tabs incl. group/container/active-time; history live + gap-fill); ephemeral background ✓ (state in `storage.session`/`storage.local`, `alarms` for the periodic net, listeners top-level, heartbeat < 30s with in-code rationale); seq/ack + buffering ✓ (durable queue, coalesced snapshots, capped buffer); per-target manifests + `gecko.id` ✓; Firefox enhancement layer feature-detected ✓ (`makeContainerResolver`); backend follow-ups ✓ (Tasks 1–2 close both Important items from the M1 final review, plus the origin-decision comment and `--limit` help minors). Deferred by design: popup/badges, sessions, content scripts, screenshots (M3+); Origin allowlist stays deferred (recorded in spec).
- **Type consistency:** `TabRecord`/`HistoryVisit`/envelope field names match backend `models.py` snake_case exactly (they are the wire format); `StorageArea`, `BrowserTab`, `WsLike`, `SyncDeps` signatures line up across Tasks 4–10; test counts accumulate 1→3→8→13→16→22→27.
- **Known simplifications (deliberate):** `history.onVisited` + `lastVisitTime` approximates per-visit times (authoritative backfill remains `buddy import-history`); the FIFO inflight protocol-correspondence assumption is documented in ws.ts and matched by the backend's strictly-sequential reply loop; background wiring is smoke-tested rather than unit-tested — all logic lives in the tested modules.

