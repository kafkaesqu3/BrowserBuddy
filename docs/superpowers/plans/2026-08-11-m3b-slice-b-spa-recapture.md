# BrowserBuddy M3b Slice B (SPA recapture & minimal dwell) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop losing SPA content changes (today `content.ts` extracts a page exactly once and never looks again) by adding change-driven recapture, and add a crude, focus-gated per-page dwell figure sufficient to unblock Slice C's screenshot gating later — without building the full scroll/visibility/idle-API dwell system the roadmap originally sketched (nothing currently planned needs that precision).

**Architecture:** Additive schema v5 (`page_visits.dwell_ms`) + one new protocol event (`page_dwell`). `content.ts` becomes a persistent per-document watcher: a `navigation.navigatesuccess` listener plus a decaying poll (2s→5s→10s→20s→40s) re-run the existing Readability extraction and resubmit only when text length changed by more than a threshold (reusing the existing `page_capture` event — a resubmit is just a fresh capture at the current URL/timestamp, same as a reload already produces). Dwell reuses the Tier-1 `tabs.ts` `active_ms` tracker (focus-based, already solves reconnect/refresh/gap-filling): `background.ts` snapshots it per tab at each page boundary and flushes the delta as an additive `page_dwell` increment, so a crashed tab/browser loses only the most recent unflushed increment, not the whole figure. Everything is gated on an active session (unknown session → `NoActiveSessionError` → error frame, existing pattern).

**Tech Stack:** existing stacks — Python (FastAPI, Pydantic, SQLite, Typer, pytest) and TypeScript (Vitest, happy-dom, Vite). No new dependencies, no new WebExtension permissions (the Navigation API is a page-level web API, not a permission).

**Spec:** `docs/superpowers/specs/2026-08-11-m3b-slice-b-spa-recapture-design.md`

## Global Constraints

- Ms epoch UTC timestamps throughout (existing convention).
- Extension imports never use `../`; use `@src/*` in extension tests (existing convention, see `vite.config.ts`'s `@src` alias).
- Backend "no active session" signal is `NoActiveSessionError` (a `LookupError` subclass), caught by the WS loop and turned into an error frame — never let it propagate as a 500 or drop the connection.
- Protocol is additive under v1: older backends error-frame unknown event types; clients skip events the server errors on. Never bump `PROTOCOL_VERSION` for an additive change.
- Backend gate (from `backend/`): `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.
- Extension gate (from `extension/`): `npm run check` (runs `tsc --noEmit && eslint . && vitest run`).
- Baselines before this plan: backend suite green (schema v4); extension ~64 tests green, no `webNavigation`/manifest changes needed this slice.
- Additive migrations only: **never edit a shipped `MIGRATIONS` entry** — append a new one.
- `Storage` methods that mutate raise `NoActiveSessionError` for an unknown session id where that's the existing contract (see `add_page_visit`); this plan's `add_page_dwell` follows the *sibling* convention instead (`mark_link_clicked`'s "return False, caller/dispatcher decides" pattern) since an unmatched dwell flush is an orphan, not a protocol-level session error — the session-validity check happens one layer up in `app.py`, exactly like `link_click`.

> **Env note for agentic workers:** spawned subagents in this environment are often denied Bash for `npm`/`uv`/`git`. If a command is denied, make all file edits, then STOP and report BLOCKED with the exact command — the controller runs gates and commits. `cd` does not persist between commands; use `npm --prefix extension run ...` and `uv run --project backend ...` or a single-line `( cd backend && ... )`.

---

## File structure

```
backend/src/browserbuddy/
  db.py          # + MIGRATIONS[4] (schema v5: page_visits.dwell_ms)
  models.py      # + PageDwellEvent; union widened
  storage.py     # + add_page_dwell; search_pages SELECT gains p.dwell_ms
  app.py         # + dispatch page_dwell
  cli.py         # search's "session pages" block gains a dwell suffix
backend/tests/
  test_db.py, test_storage_dwell.py (NEW), test_models.py, test_app.py, test_cli.py

extension/src/
  types.ts       # + page_dwell EventPayload arm
  recapture.ts   # NEW: RECAPTURE_DELAYS_MS, delayForStep(step), textChangedEnough(a,b)
  capture.ts     # + pageDwellEvent(url, dwellMs, now)
  tabs.ts        # + TabTracker.activeMs(tabId): read-only banked-time snapshot
  content.ts     # persistent recapture watcher (navigatesuccess + decaying poll)
  background.ts  # + dwell rebase/flush wiring, tabId threaded into handlePageMessage
extension/tests/
  recapture.test.ts (NEW), capture.test.ts (update), tabs.test.ts (update)
```

---

## Task 1: Backend — schema v5 (`page_visits.dwell_ms`)

**Files:**
- Modify: `backend/src/browserbuddy/db.py`
- Modify: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `page_visits.dwell_ms INTEGER NOT NULL DEFAULT 0` — later tasks (`add_page_dwell`, `search_pages`) read/write this column.

- [ ] **Step 1: Write failing migration test** — append to `backend/tests/test_db.py`:

```python
def test_v5_migration_adds_dwell_column_and_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:4])
    conn = db.connect(path)  # v1-v4 only
    conn.execute("INSERT INTO sessions (client_id, name, started_at) VALUES ('c', 's', 1)")
    conn.execute(
        "INSERT INTO page_visits (session_id, url, visited_at) VALUES (1, 'https://a.com', 10)"
    )
    conn.commit()
    conn.close()
    monkeypatch.undo()
    conn = db.connect(path)  # applies v5 (and any later migrations) on top of v1-v4 data
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 5
    assert conn.execute("SELECT dwell_ms FROM page_visits").fetchone()[0] == 0  # backfills to 0
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_db.py -v -k v5`
Expected: FAIL — `MAX(version)` is 4; `no such column: dwell_ms`.

- [ ] **Step 3: Append migration v5 to `MIGRATIONS` in `backend/src/browserbuddy/db.py`** (add a new string element after the v4 script; do not edit v1-v4):

```python
    # v5: per-page dwell time (Slice B). Accumulated additively as the
    # extension flushes focus-gated active-time increments at page boundaries
    # (see background.ts rebaseDwell/flushDwell) — never set outright, so a
    # crashed tab loses only its most recent unflushed increment.
    """
    ALTER TABLE page_visits ADD COLUMN dwell_ms INTEGER NOT NULL DEFAULT 0;
    """,
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_db.py -v -k v5`: PASS. Full file green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/db.py backend/tests/test_db.py
git commit -m "Add schema v5 with page_visits.dwell_ms"
```

---

## Task 2: Backend — `add_page_dwell` storage method

**Files:**
- Modify: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/test_storage_dwell.py`

**Interfaces:**
- Consumes: `page_visits.dwell_ms` (Task 1).
- Produces: `Storage.add_page_dwell(session_id: int, *, url: str, dwell_ms: int) -> bool` — Task 4 (`app.py`) calls this.

- [ ] **Step 1: Write failing tests** — create `backend/tests/test_storage_dwell.py`:

```python
def _page(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    pid = storage.add_page_visit(
        sid,
        url="https://a.com",
        title="A",
        visited_at=1100,
        text="",
        html_key=None,
        container=None,
    )
    return sid, pid


def test_add_page_dwell_accumulates_on_latest_visit(storage):
    sid, pid = _page(storage)
    assert storage.add_page_dwell(sid, url="https://a.com", dwell_ms=3000) is True
    assert storage.add_page_dwell(sid, url="https://a.com", dwell_ms=1500) is True
    row = storage._conn.execute(
        "SELECT dwell_ms FROM page_visits WHERE id = ?", (pid,)
    ).fetchone()
    assert row["dwell_ms"] == 4500


def test_add_page_dwell_targets_the_latest_visit_for_the_url(storage):
    sid, first = _page(storage)
    second = storage.add_page_visit(
        sid,
        url="https://a.com",
        title="A2",
        visited_at=1200,
        text="",
        html_key=None,
        container=None,
    )
    storage.add_page_dwell(sid, url="https://a.com", dwell_ms=999)
    rows = {
        r["id"]: r["dwell_ms"]
        for r in storage._conn.execute("SELECT id, dwell_ms FROM page_visits").fetchall()
    }
    assert rows[second] == 999
    assert rows[first] == 0


def test_add_page_dwell_orphan_returns_false(storage):
    sid, _ = _page(storage)
    assert storage.add_page_dwell(sid, url="https://never-seen.com", dwell_ms=100) is False
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_storage_dwell.py -v`
Expected: FAIL — `add_page_dwell` missing.

- [ ] **Step 3: Append to the `Storage` class in `backend/src/browserbuddy/storage.py`** (after `list_unfollowed_links`, at the end of the file):

```python
    def add_page_dwell(self, session_id: int, *, url: str, dwell_ms: int) -> bool:
        """Add dwell_ms to the latest page_visit for url in this session.

        Additive, not a set — the extension flushes small increments over
        time (not one final value), so a crashed tab/browser only loses the
        most recent unflushed increment. Returns False when the page was
        never captured (orphan flush, same convention as
        mark_link_clicked's orphan-click handling — dropped by the caller).
        """
        row = self._conn.execute(
            "SELECT id FROM page_visits WHERE session_id = ? AND url = ?"
            " ORDER BY visited_at DESC LIMIT 1",
            (session_id, url),
        ).fetchone()
        if row is None:
            return False
        with self._conn:
            self._conn.execute(
                "UPDATE page_visits SET dwell_ms = dwell_ms + ? WHERE id = ?",
                (dwell_ms, row["id"]),
            )
        return True
```

Also update `search_pages` so the CLI (Task 5) can display dwell. Change the `SELECT` clause from:

```python
            SELECT p.url, p.title, p.visited_at, s.name AS session_name
```

to:

```python
            SELECT p.url, p.title, p.visited_at, p.dwell_ms, s.name AS session_name
```

(This is the only change to `search_pages` — same method, same `FROM`/`WHERE`/`ORDER BY`/`LIMIT`.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_storage_dwell.py tests/test_storage_sessions.py -q`: PASS (existing `search_pages` tests access columns by name, so the added column doesn't break them). `uv run ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/storage.py backend/tests/test_storage_dwell.py
git commit -m "Add add_page_dwell storage method"
```

---

## Task 3: Backend — `PageDwellEvent` protocol model

**Files:**
- Modify: `backend/src/browserbuddy/models.py`
- Modify: `backend/tests/test_models.py`

**Interfaces:**
- Produces: `PageDwellEvent { type: "page_dwell", url: str, dwell_ms: int (ge=0), occurred_at: int (ge=0) }` — Task 4 (`app.py`) dispatches on this type.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_models.py`:

```python
def test_page_dwell_validates_and_defaults():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "page_dwell",
                "url": "https://a.com",
                "dwell_ms": 4200,
                "occurred_at": 9,
            },
        }
    )
    assert env.event.type == "page_dwell"
    assert env.event.dwell_ms == 4200


def test_page_dwell_rejects_negative_dwell():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "v": 1,
                "seq": 1,
                "event": {
                    "type": "page_dwell",
                    "url": "https://a.com",
                    "dwell_ms": -1,
                    "occurred_at": 1,
                },
            }
        )
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_models.py -v -k page_dwell`: FAIL (unknown event type `page_dwell`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/models.py`**

Add the new event (after `NavEdgeEvent`):

```python
class PageDwellEvent(ProtocolModel):
    type: Literal["page_dwell"]
    url: str = Field(min_length=1)
    dwell_ms: int = Field(ge=0)
    occurred_at: int = Field(ge=0)
```

Widen the envelope union:

```python
    event: (
        TabStateEvent
        | HistoryVisitsEvent
        | SessionStartEvent
        | SessionEndEvent
        | PageCaptureEvent
        | LinkClickEvent
        | NavEdgeEvent
        | PageDwellEvent
    ) = Field(discriminator="type")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_models.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/models.py backend/tests/test_models.py
git commit -m "Add PageDwellEvent protocol model"
```

---

## Task 4: Backend — ingest dispatch for `page_dwell`

**Files:**
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_app.py`

**Interfaces:**
- Consumes: `PageDwellEvent` (Task 3), `Storage.add_page_dwell` (Task 2).

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_app.py`:

```python
def test_ws_page_dwell_accumulates_on_page_visit(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(
                2,
                {"type": "page_capture", "url": "https://a.com", "visited_at": 150, "text": "b"},
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_dwell",
                    "url": "https://a.com",
                    "dwell_ms": 4000,
                    "occurred_at": 160,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
        ws.send_json(
            _envelope(
                4,
                {
                    "type": "page_dwell",
                    "url": "https://a.com",
                    "dwell_ms": 1500,
                    "occurred_at": 170,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 4}
    row = app.state.storage._conn.execute("SELECT dwell_ms FROM page_visits").fetchone()
    assert row["dwell_ms"] == 5500  # additive across two flushes


def test_ws_page_dwell_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                1, {"type": "page_dwell", "url": "https://a.com", "dwell_ms": 1, "occurred_at": 1}
            )
        )
        assert ws.receive_json()["type"] == "error"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_app.py -v -k page_dwell`: FAIL (`unhandled event type: PageDwellEvent`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/app.py`**

Add `PageDwellEvent` to the models import:

```python
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    LinkClickEvent,
    NavEdgeEvent,
    PageCaptureEvent,
    PageDwellEvent,
    SessionEndEvent,
    SessionStartEvent,
    TabStateEvent,
)
```

In `_apply_event`, add a branch after the `LinkClickEvent` branch (before the final `else`):

```python
    elif isinstance(event, PageDwellEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_dwell with no active session")
        storage.add_page_dwell(active["id"], url=event.url, dwell_ms=event.dwell_ms)
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_app.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py
git commit -m "Dispatch page_dwell ingest"
```

---

## Task 5: Backend — dwell display in `buddy search`

**Files:**
- Modify: `backend/src/browserbuddy/cli.py`
- Modify: `backend/tests/test_cli.py`

**Interfaces:**
- Consumes: `search_pages` rows now carrying `dwell_ms` (Task 2).

- [ ] **Step 1: Write failing test** — append to `backend/tests/test_cli.py`:

```python
def test_search_shows_dwell_when_present(tmp_path):
    from browserbuddy import db
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    s = Storage(conn)
    sid = s.start_session("ext-1", "vector research", started_at=1720000000000)
    s.add_page_visit(
        sid,
        url="https://arxiv.org/abs/8",
        title="HNSW explained",
        visited_at=1720000001000,
        text="hierarchical navigable small worlds",
        html_key=None,
        container=None,
    )
    s.add_page_dwell(sid, url="https://arxiv.org/abs/8", dwell_ms=42_000)
    conn.close()
    result = runner.invoke(app, ["search", "navigable"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "dwell=42s" in result.output


def test_search_omits_dwell_suffix_when_zero(tmp_path):
    _seed_session(tmp_path)  # existing helper: seeds a page with no dwell recorded
    result = runner.invoke(app, ["search", "navigable"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "dwell=" not in result.output
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_cli.py -v -k dwell`: FAIL (no dwell suffix rendered; `test_search_shows_dwell_when_present` fails since `dwell=42s` is absent — `test_search_omits_dwell_suffix_when_zero` already passes trivially, but run both to confirm the first fails).

- [ ] **Step 3: Edit `backend/src/browserbuddy/cli.py`**

In the `search` command, replace the per-page loop body:

```python
    if pages:
        typer.echo("-- session pages --")
        for row in pages:
            typer.echo(
                f"{_fmt_time(row['visited_at'])}  {row['title'] or '(untitled)'}"
                f"  [{row['session_name']}]"
            )
            typer.echo(f"                  {row['url']}")
```

with:

```python
    if pages:
        typer.echo("-- session pages --")
        for row in pages:
            dwell = f"  dwell={row['dwell_ms'] // 1000}s" if row["dwell_ms"] else ""
            typer.echo(
                f"{_fmt_time(row['visited_at'])}  {row['title'] or '(untitled)'}"
                f"  [{row['session_name']}]{dwell}"
            )
            typer.echo(f"                  {row['url']}")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_cli.py -q`, then the full backend suite `uv run pytest -q`. Both green. `uv run ruff check . && uv run ruff format --check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/cli.py backend/tests/test_cli.py
git commit -m "Show per-page dwell time in buddy search"
```

---

## Task 6: Extension — `recapture.ts` (decay schedule + change threshold)

**Files:**
- Create: `extension/src/recapture.ts`
- Create: `extension/tests/recapture.test.ts`

**Interfaces:**
- Produces: `RECAPTURE_DELAYS_MS: number[]`, `delayForStep(step: number): number`, `textChangedEnough(lastLength: number, newLength: number): boolean` — Task 9 (`content.ts`) wires these into the poll loop.

- [ ] **Step 1: Write failing tests** — create `extension/tests/recapture.test.ts`:

```ts
import { expect, test } from "vitest";
import { RECAPTURE_DELAYS_MS, delayForStep, textChangedEnough } from "@src/recapture";

test("delayForStep steps through the schedule and clamps at the last entry", () => {
  expect(delayForStep(0)).toBe(2000);
  expect(delayForStep(1)).toBe(5000);
  expect(delayForStep(4)).toBe(40000);
  expect(delayForStep(99)).toBe(40000); // clamped, not out of bounds
});

test("delayForStep clamps negative steps to the first interval", () => {
  expect(delayForStep(-1)).toBe(RECAPTURE_DELAYS_MS[0]);
});

test("textChangedEnough requires >20% delta on a long page", () => {
  expect(textChangedEnough(10000, 11900)).toBe(false); // 19% — below threshold
  expect(textChangedEnough(10000, 12001)).toBe(true); // just over 20%
});

test("textChangedEnough uses the 200-char floor on a short page", () => {
  expect(textChangedEnough(100, 250)).toBe(false); // 150 chars, below the 200 floor
  expect(textChangedEnough(100, 350)).toBe(true); // 250 chars, over the floor
});

test("textChangedEnough is symmetric (shrinking content also counts)", () => {
  expect(textChangedEnough(10000, 7000)).toBe(true); // -30%
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- recapture`: FAIL (module missing).

- [ ] **Step 3: Create `extension/src/recapture.ts`**

```ts
// SPA change-driven recapture: a decaying poll schedule plus a text-length
// delta threshold, so a route change under the same URL (or any DOM
// mutation that materially rewrites the page) gets re-extracted without
// re-triggering on trivial noise (a ticking view counter, a live
// timestamp).

/** Poll delays, in ms, stepped through on consecutive no-change ticks and
 * capped at the last entry. content.ts resets to step 0 whenever a change
 * is sent (bursty updates keep getting caught quickly). */
export const RECAPTURE_DELAYS_MS = [2000, 5000, 10000, 20000, 40000];

export function delayForStep(step: number): number {
  const idx = Math.min(Math.max(step, 0), RECAPTURE_DELAYS_MS.length - 1);
  return RECAPTURE_DELAYS_MS[idx]!;
}

/** True when the extracted text length changed enough to be worth
 * re-sending: more than 20% of the previous length, or more than 200
 * chars, whichever is larger. The 200-char floor catches a route change on
 * a short page; the 20% relative threshold ignores a ticking counter on a
 * long article. Symmetric — a shrinking page counts too. */
export function textChangedEnough(lastLength: number, newLength: number): boolean {
  const delta = Math.abs(newLength - lastLength);
  return delta > Math.max(0.2 * lastLength, 200);
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- recapture`: PASS. `npm --prefix extension run check`: clean (new module, no consumers yet).

- [ ] **Step 5: Commit**

```bash
git add extension/src/recapture.ts extension/tests/recapture.test.ts
git commit -m "Add SPA recapture decay-schedule and change-threshold helpers"
```

---

## Task 7: Extension — `page_dwell` type + `pageDwellEvent` conversion

**Files:**
- Modify: `extension/src/types.ts`
- Modify: `extension/src/capture.ts`
- Modify: `extension/tests/capture.test.ts`

**Interfaces:**
- Produces: `pageDwellEvent(url: string, dwellMs: number, now?: () => number): EventPayload & { type: "page_dwell" }` — Task 10 (`background.ts`) calls this.

- [ ] **Step 1: Write failing test** — in `extension/tests/capture.test.ts`, update the top-of-file import from:

```ts
import { MAX_HTML_CHARS, MAX_LINKS, linkClickEvent, pageCaptureEvent } from "@src/capture";
```

to:

```ts
import { MAX_HTML_CHARS, MAX_LINKS, linkClickEvent, pageCaptureEvent, pageDwellEvent } from "@src/capture";
```

Then append:

```ts
test("pageDwellEvent builds a page_dwell event", () => {
  expect(pageDwellEvent("https://a.com", 4200, () => 1_000)).toEqual({
    type: "page_dwell",
    url: "https://a.com",
    dwell_ms: 4200,
    occurred_at: 1_000,
  });
});

test("pageDwellEvent clamps a negative dwell to 0", () => {
  expect(pageDwellEvent("https://a.com", -5, () => 1_000).dwell_ms).toBe(0);
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- capture`: FAIL (`pageDwellEvent` missing; `page_dwell` not on `EventPayload`).

- [ ] **Step 3a: Edit `extension/src/types.ts`** — add the `page_dwell` arm to `EventPayload`. Replace the closing of the `nav_edge` arm:

```ts
      query: string | null;
      occurred_at: number;
    };
```

with (the terminating `;` moves to the new `page_dwell` arm):

```ts
      query: string | null;
      occurred_at: number;
    }
  | {
      type: "page_dwell";
      url: string;
      dwell_ms: number;
      occurred_at: number;
    };
```

- [ ] **Step 3b: Append to `extension/src/capture.ts`**

```ts
/** Build a page_dwell event from a locally-computed dwell increment (there is
 * no incoming content-script message to convert — background.ts derives the
 * increment itself from the Tier-1 tab-active-time tracker; see tabs.ts). */
export function pageDwellEvent(
  url: string,
  dwellMs: number,
  now: () => number = Date.now,
): EventPayload & { type: "page_dwell" } {
  return { type: "page_dwell", url, dwell_ms: Math.max(0, dwellMs), occurred_at: now() };
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- capture` (PASS), then `npm --prefix extension run check` (tsc clean — `types.ts` declares `page_dwell` and `capture.ts` supplies it).

- [ ] **Step 5: Commit**

```bash
git add extension/src/types.ts extension/src/capture.ts extension/tests/capture.test.ts
git commit -m "Add page_dwell event type and pageDwellEvent conversion"
```

---

## Task 8: Extension — `TabTracker.activeMs` (read-only banked-time snapshot)

**Files:**
- Modify: `extension/src/tabs.ts`
- Modify: `extension/tests/tabs.test.ts`

**Interfaces:**
- Produces: `TabTracker.activeMs(tabId: number): Promise<number>` — Task 10 (`background.ts`) calls this to snapshot/diff a tab's clock at page boundaries.

- [ ] **Step 1: Write failing tests** — append to `extension/tests/tabs.test.ts`:

```ts
test("activeMs reads current banked time without mutating state", async () => {
  const c = clock();
  const tracker = new TabTracker(fakeStorageArea(), c.now);
  await tracker.recordActivation(1, 1);
  c.advance(4000);
  expect(await tracker.activeMs(1)).toBe(4000); // includes the live open interval
  c.advance(1000);
  await tracker.recordActivation(2, 1); // switch away: tab 1 banks 5000 total
  expect(await tracker.activeMs(1)).toBe(5000);
  expect(await tracker.activeMs(2)).toBe(0); // just activated
});

test("activeMs returns 0 for an unknown tab", async () => {
  const tracker = new TabTracker(fakeStorageArea(), () => 1000);
  expect(await tracker.activeMs(999)).toBe(0);
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- tabs`: FAIL (`activeMs` missing).

- [ ] **Step 3: Append to the `TabTracker` class in `extension/src/tabs.ts`** (after `snapshot`/`snapshotImpl`, at the end of the class):

```ts
  /** Current banked active time for a tab (includes the live open interval
   * if it's the currently active tab), without mutating stored state. Used
   * by dwell accounting to snapshot/diff a tab's clock at page boundaries
   * (see background.ts rebaseDwell/flushDwell). Returns 0 for a tab this
   * tracker has never seen. */
  activeMs(tabId: number): Promise<number> {
    return this.serialize(() => this.activeMsImpl(tabId));
  }

  private async activeMsImpl(tabId: number): Promise<number> {
    const state = await this.load();
    const entry = state.perTab[String(tabId)];
    if (!entry) return 0;
    let ms = entry.active_ms;
    if (state.activeTabId === tabId && state.activeSince !== null) {
      ms += Math.max(0, this.now() - state.activeSince);
    }
    return ms;
  }
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- tabs`: PASS. `npm --prefix extension run check`: clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/tabs.ts extension/tests/tabs.test.ts
git commit -m "Add TabTracker.activeMs read-only banked-time snapshot"
```

---

## Task 9: Extension — content script SPA recapture watcher

**Files:**
- Modify: `extension/src/content.ts`

Content script is a thin IIFE with no unit tests (verified by build + smoke, like M3a/Slice A). Its pure logic (`textChangedEnough`, `delayForStep`) is already tested in Task 6.

- [ ] **Step 1: Replace `extension/src/content.ts`**

```ts
import { delayForStep, textChangedEnough } from "./recapture";
import { extractLinks, extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts the page + its
// outbound-link catalogue once the page settles, keeps watching for SPA
// content changes (Navigation API + a decaying poll) for as long as the
// document lives, and reports every anchor click (including non-navigating
// ones the background's webNavigation cannot see: downloads, JS handlers,
// new-tab opens).
(() => {
  const runtime = (globalThis as { browser?: typeof browser }).browser?.runtime;
  let lastSentLength = 0;
  let step = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const send = (page = extractPage(document)) => {
    const links = extractLinks(document);
    lastSentLength = page.text.length;
    void runtime
      ?.sendMessage({ type: "bb:page", url: location.href, ...page, links })
      .catch(() => undefined); // background asleep/unreachable: drop silently
  };

  const scheduleRecheck = () => {
    clearTimeout(timer);
    timer = setTimeout(recheck, delayForStep(step));
  };

  // Re-extract and compare to the last *sent* length; resubmit (a fresh
  // page_capture, same shape a reload already produces) only on a material
  // change, resetting the poll schedule to its fastest step so bursty
  // updates keep getting caught quickly. Otherwise let the interval decay.
  const recheck = () => {
    const page = extractPage(document);
    if (textChangedEnough(lastSentLength, page.text.length)) {
      step = 0;
      send(page);
    } else {
      step += 1;
    }
    scheduleRecheck();
  };

  const onClick = (event: MouseEvent) => {
    const target = event.target as Element | null;
    const anchor = target?.closest?.("a[href]") as HTMLAnchorElement | null;
    if (!anchor || !/^https?:/i.test(anchor.href)) return;
    void runtime
      ?.sendMessage({
        type: "bb:click",
        page_url: location.href,
        href: anchor.href,
        text: (anchor.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 300),
      })
      .catch(() => undefined);
  };
  // Capture phase so we still see the click if a handler calls stopPropagation;
  // auxclick covers middle-click "open in new tab".
  addEventListener("click", onClick, true);
  addEventListener("auxclick", onClick, true);

  const start = () => {
    setTimeout(send, 500);
    scheduleRecheck();
  };
  if (document.readyState === "complete") {
    start();
  } else {
    addEventListener("load", start, { once: true });
  }

  // Navigation API: an immediate recheck trigger for SPA route changes,
  // bypassing the wait for the next poll tick. Cast avoids depending on
  // Navigation API lib types (not universally present); absent on browsers
  // that don't support it — the decaying poll is the fallback.
  const nav = (
    globalThis as { navigation?: { addEventListener: (type: string, cb: () => void) => void } }
  ).navigation;
  nav?.addEventListener("navigatesuccess", () => {
    step = 0;
    clearTimeout(timer);
    timer = setTimeout(recheck, 500); // let the new route settle, like initial load
  });
})();
```

- [ ] **Step 2: Run** — `npm --prefix extension run check` (still clean, same test count as after Task 8 — no new tests for this file) and `npm --prefix extension run build`; confirm `dist/firefox/content.js` exists and is an IIFE (starts with `(function` / `var`), not an `import`.

- [ ] **Step 3: Commit**

```bash
git add extension/src/content.ts
git commit -m "Add SPA change-driven recapture to the content script"
```

---

## Task 10: Extension — background dwell rebase/flush wiring

**Files:**
- Modify: `extension/src/background.ts`

Background wiring; verified by build + smoke (pure helpers already tested in Tasks 6-8). Dwell state lives in `storage.session` (survives worker death, resets with the browser — same lifetime as the existing per-tab nav-url map).

- [ ] **Step 1: Edit `extension/src/background.ts`**

Change the capture import (currently `import { linkClickEvent, pageCaptureEvent, type ClickMessage, type PageMessage } from "./capture";`) to:

```ts
import {
  linkClickEvent,
  pageCaptureEvent,
  pageDwellEvent,
  type ClickMessage,
  type PageMessage,
} from "./capture";
```

Replace `handlePageMessage` (currently taking `msg, cookieStoreId`) with a version that also accepts the tab id and rebases dwell after a successful capture:

```ts
async function handlePageMessage(
  msg: PageMessage,
  cookieStoreId: string | undefined,
  tabId: number | undefined,
): Promise<void> {
  const container = await containerName(cookieStoreId);
  const event = pageCaptureEvent(msg, container);
  if (!event) return;
  // Recording check happens LAST, immediately before enqueue — a stop command
  // racing this handler must not slip a capture in behind the session_end event
  // (narrows the check-to-enqueue window to zero awaits).
  const state = await sessionManager.current();
  if (state?.status !== "recording") return; // stale content script or paused
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
  if (tabId !== undefined) {
    await sendDwell(await rebaseDwell(tabId, event.url));
  }
}
```

Add the dwell helpers directly after the existing `forgetTab` function (i.e., right before `async function handleCommitted`):

```ts
const DWELL_KEY = "bb:dwell"; // per-tab { url, baseline } snapshot for dwell accounting

// Same non-transactional-storage hazard as the nav-url map above.
let dwellMutex: Promise<unknown> = Promise.resolve();
function dwellSerialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = dwellMutex.then(fn, fn);
  dwellMutex = next.catch(() => undefined);
  return next;
}

interface DwellEntry {
  url: string;
  baseline: number;
}

async function readDwellMap(): Promise<Record<number, DwellEntry>> {
  return ((await session.get(DWELL_KEY))[DWELL_KEY] as Record<number, DwellEntry>) ?? {};
}

/** Flush the previous page's dwell (if any) for this tab and start tracking
 * the new one from the tab's current active-time baseline (the Tier-1
 * focus-based tracker in tabs.ts). Returns the flushed increment, or null
 * when there was nothing to flush (first page in a fresh tab) or the
 * elapsed time was zero (tab was never focused since the last boundary). */
function rebaseDwell(
  tabId: number,
  newUrl: string,
): Promise<{ url: string; dwellMs: number } | null> {
  return dwellSerialize(async () => {
    const map = await readDwellMap();
    const prev = map[tabId];
    const now = await tracker.activeMs(tabId);
    map[tabId] = { url: newUrl, baseline: now };
    await session.set({ [DWELL_KEY]: map });
    if (!prev) return null;
    const dwellMs = Math.max(0, now - prev.baseline);
    return dwellMs > 0 ? { url: prev.url, dwellMs } : null;
  });
}

/** Flush a tab's current dwell without starting a new tracking entry — used
 * on tab close, where there is no "next page" to rebase onto. */
function flushDwell(tabId: number): Promise<{ url: string; dwellMs: number } | null> {
  return dwellSerialize(async () => {
    const map = await readDwellMap();
    const prev = map[tabId];
    if (!prev) return null;
    delete map[tabId];
    await session.set({ [DWELL_KEY]: map });
    const now = await tracker.activeMs(tabId);
    const dwellMs = Math.max(0, now - prev.baseline);
    return dwellMs > 0 ? { url: prev.url, dwellMs } : null;
  });
}

/** Flush every tracked tab's dwell — called on session stop so the final
 * page of each open tab isn't lost (nothing else triggers a rebase for it
 * once the session ends). */
async function flushAllDwell(): Promise<void> {
  const map = await dwellSerialize(async () => {
    const m = await readDwellMap();
    await session.set({ [DWELL_KEY]: {} });
    return m;
  });
  for (const [tabIdStr, entry] of Object.entries(map)) {
    const now = await tracker.activeMs(Number(tabIdStr));
    const dwellMs = Math.max(0, now - entry.baseline);
    if (dwellMs > 0) await sendDwell({ url: entry.url, dwellMs });
  }
}

async function sendDwell(flush: { url: string; dwellMs: number } | null): Promise<void> {
  if (!flush) return;
  await queue.enqueue(pageDwellEvent(flush.url, flush.dwellMs));
  await (await ensureClient()).notifyEnqueued();
}
```

Flush all tracked dwell on session stop — in `handleSessionCommand`, change the `"stop"` case from:

```ts
      case "stop": {
        await sessionManager.stop(async (event) => {
          await queue.enqueue(event);
          await (await ensureClient()).notifyEnqueued();
        });
        break;
      }
```

to (dwell flushes must be enqueued *before* `session_end` so the backend still has an active session when it processes them — local queue order is WS send order):

```ts
      case "stop": {
        await flushAllDwell();
        await sessionManager.stop(async (event) => {
          await queue.enqueue(event);
          await (await ensureClient()).notifyEnqueued();
        });
        break;
      }
```

Flush on tab close — change the `browser.tabs.onRemoved` listener from:

```ts
browser.tabs.onRemoved.addListener((tabId) => {
  scheduleSnapshot();
  logged(forgetTab(tabId));
});
```

to:

```ts
browser.tabs.onRemoved.addListener((tabId) => {
  scheduleSnapshot();
  logged(forgetTab(tabId));
  logged(flushDwell(tabId).then(sendDwell));
});
```

Thread the tab id into the `bb:page` handler — change:

```ts
  if (msg?.type === "bb:page") {
    logged(handlePageMessage(msg as PageMessage, sender.tab?.cookieStoreId));
    return false; // no reply — content script fire-and-forget
  }
```

to:

```ts
  if (msg?.type === "bb:page") {
    logged(handlePageMessage(msg as PageMessage, sender.tab?.cookieStoreId, sender.tab?.id));
    return false; // no reply — content script fire-and-forget
  }
```

- [ ] **Step 2: Run** — `npm --prefix extension run check` (same test count as after Task 9, clean — no new tests for this file; tsc/eslint verify the wiring compiles) and `npm --prefix extension run build`.

- [ ] **Step 3: Commit**

```bash
git add extension/src/background.ts
git commit -m "Flush per-page dwell from the Tier-1 tab-active-time tracker"
```

---

## Task 11: Gates, automated e2e, docs, smoke instructions

**Files:** `README.md` (modify); otherwise verification only.

- [ ] **Step 1: Full gates, both projects**

```bash
cd /Users/david/Projects/BrowserBuddy/extension && npm run check && npm run build && npx web-ext lint --source-dir dist/firefox
cd /Users/david/Projects/BrowserBuddy/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: extension clean (~72 tests: prior baseline + `recapture.test.ts` + capture/tabs additions); backend clean (prior baseline + Tasks 1-5 additions). `web-ext lint`: the pre-existing `data_collection_permissions` + bundled-Readability `UNSAFE_VAR_ASSIGNMENT` warnings are known/acceptable — report any new ones.

- [ ] **Step 2: Automated protocol e2e** (same pattern as Slice A's Task 13): scratch Node WS client against a temp-data-dir `buddy serve` on an unused port —

1. auth, `session_start`, `page_capture` for `https://a.com` — expect acks.
2. `page_dwell { url: "https://a.com", dwell_ms: 3000 }`, then `page_dwell { url: "https://a.com", dwell_ms: 1500 }` — expect acks.
3. `session_end` — expect ack.
4. `page_dwell` again (same url) — expect an **error frame** (no active session).

Verify via `BROWSERBUDDY_DATA_DIR=… uv run buddy search "<phrase in the captured text>"` that the session-pages line shows `dwell=4s` (4500ms truncated to whole seconds). Kill server, clean up. Report verbatim output.

- [ ] **Step 3: Update `README.md`** — add one line after the existing `buddy links` paragraph (around line 47):

```markdown

Pages you dwell on show up in `buddy search`'s results too (`dwell=42s`) —
a crude, focus-gated figure from the same tab-active-time clock Tier-1
already tracks, not a precise reading-time measurement.
```

Commit:

```bash
cd /Users/david/Projects/BrowserBuddy
git add README.md
git commit -m "Document per-page dwell display in buddy search"
```

- [ ] **Step 4: Hand off the interactive smoke to the user** (not automatable): via `./scripts/smoke.sh --fresh --build`, pair, start a session. Then:
  - **SPA recapture:** visit a client-side-routed site (e.g. a Wikipedia article, then use its search-as-you-type or a SPA news site) and navigate between routes *without* a full page reload; wait through at least one poll interval (or trigger a route change the Navigation API catches immediately). Stop. Run `uv run buddy search "<phrase only on the second route>"` and confirm it's found under the *same* session (a new `page_visit` row was captured for the route change, not just the initial load).
  - **Dwell:** in the same session, deliberately switch away from the tab (to another tab or app) for a while, then switch back before Stop. Run `uv run buddy search "<phrase from that page>"` and confirm the printed `dwell=Ns` is roughly the *focused* time only (not including the time spent away).
  - **Regression:** confirm outbound links / click tracking / nav edges from Slice A still work (`buddy links "<name>"`), and pause still captures nothing (Slice 0/M3a).

---

## Self-review notes

- **Spec coverage:** schema v5 `page_visits.dwell_ms` (Task 1) — spec §1. `add_page_dwell` additive latest-visit lookup, mirroring `mark_link_clicked` (Task 2) — spec §4. `PageDwellEvent` protocol model (Task 3) — spec §2. Dispatch gated on active session (Task 4) — spec §4/error-handling. CLI dwell display in `buddy search` (Task 5) — spec §4. Text-length-delta decision function + decay-schedule stepper as pure/tested units (Task 6) — spec §3/§4 testing split. `pageDwellEvent` conversion (Task 7) — spec §3. `TabTracker.activeMs` reused Tier-1 clock, no new content-script listeners/idle API/scroll signal (Task 8) — spec §3 ("no new content-script listeners... exactly the existing Tier-1 clock, sliced at page boundaries"). Content-script `navigatesuccess` + decaying poll + resubmit-on-change (Task 9) — spec §3. Background rebase/flush at page-capture, tab-close, and session-stop boundaries, enqueued before `session_end` (Task 10) — spec §3/§4. Deferred items (idle API, scroll/visibility precision, MutationObserver, dwell-gated screenshots, embeddings/digests/recall agent) are out of scope and absent by design (spec "Out of scope for Slice B").
- **Type consistency:** `PageDwellEvent` fields (`url: str, dwell_ms: int (ge=0), occurred_at: int (ge=0)`) match across `models.py`, `app.py`, `storage.add_page_dwell`'s keyword args, `types.ts`'s `page_dwell` arm (`url: string, dwell_ms: number, occurred_at: number`), and `capture.ts`'s `pageDwellEvent`. `TabTracker.activeMs(tabId: number): Promise<number>` return type matches its two call sites in `background.ts` (`rebaseDwell`, `flushDwell`, `flushAllDwell`), all of which do `now - baseline` arithmetic on the same unit (ms). `handlePageMessage`'s new third parameter (`tabId: number | undefined`) matches its one call site in the `runtime.onMessage` listener (`sender.tab?.id`).
- **Behavior preserved:** the `page_dwell` dispatch branch gates on an active session and raises `NoActiveSessionError` (caught by the WS loop → error frame, no ack — existing semantics, same as `nav_edge`/`link_click`). Additive protocol under v1 — no `PROTOCOL_VERSION` bump. Idempotency for `page_dwell` (e.g. a replayed WS message after a reconnect) comes from the existing `envelope.seq > last_seq` cursor check in `app.py` — not from any new dedup logic in storage — consistent with how every other event type is already protected; `add_page_dwell` itself is intentionally unconditionally additive (a genuinely new flush is a genuinely new increment).
- **Known simplifications (deliberate, per spec's trimmed scope):** dwell is focus-gated only (reuses `tabs.ts`'s existing clock) — no idle-API integration, so a focused-but-AFK tab still accrues dwell; no scroll/visibility precision. Same-URL-in-two-tabs ambiguity in `add_page_dwell`'s "latest visit" lookup is carried forward from the identical, already-accepted tradeoff in `mark_link_clicked`. SPA change detection is a text-length threshold + a fallback poll — no MutationObserver, so extremely bursty near-simultaneous edits within one poll window resolve to whatever the extracted text looks like at the tick, not every intermediate state (acceptable: the point is not losing content permanently, not capturing every transient frame).
