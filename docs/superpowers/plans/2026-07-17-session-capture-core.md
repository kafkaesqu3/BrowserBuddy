# BrowserBuddy Session Capture Core (M3a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explicit research sessions end to end — start/pause/stop from a toolbar popup, Readability text + raw-HTML capture of every page read during a session (content-addressed blob storage), sessions and page text searchable from the CLI.

**Architecture:** Backend gains schema v2 (`sessions`, `page_visits` + FTS5) plus a gzip content-addressed `BlobStore`, and three new protocol events (`session_start`, `session_end`, `page_capture`) dispatched by the existing WS loop. The extension gains a mutex-serialized `SessionManager` (persisted in `storage.local`), a dynamically registered content script (Readability + raw `innerHTML`, extraction logic in a testable `extract.ts`), a capture module converting content-script messages to protocol events, popup UI (start/pause/resume/stop + badge), and host-permission request on the Start gesture. Firewall is deferred (spec roadmap).

**Tech Stack:** existing stacks + `@mozilla/readability` (runtime dep — the spec's extraction engine) and `happy-dom` (dev dep, DOM for extraction tests). Content script built as a second Vite pass (IIFE — content scripts can't be modules).

**Spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**This is Plan 3 of ~6 (M3a).** Deferred to M3b: clicked/outbound links, nav edges + search provenance, per-page dwell, SPA change-driven recapture, screenshots (and the REST blob endpoint they need). Deferred to roadmap: sensitive-content firewall. Digests/agent: M4.

---

## File structure

```
backend/src/browserbuddy/
  db.py                     # + MIGRATIONS[1] (schema v2)
  blobs.py                  # NEW: gzip content-addressed store
  models.py                 # + SessionStartEvent, SessionEndEvent, PageCaptureEvent
  storage.py                # + session/page methods
  app.py                    # + event dispatch, BlobStore wiring, LookupError→error frame
  cli.py                    # + `buddy sessions`; search covers pages
backend/tests/
  test_db.py (migration test), test_blobs.py, test_models.py,
  test_storage_sessions.py, test_app.py, test_cli.py

extension/src/
  tabs.ts                   # M2 hardening: active_ms clamp
  ws.ts                     # M2 hardening: null onmessage/onopen in teardown
  background.ts             # session lifecycle wiring, bb:page handling, badge
  sessions.ts               # NEW: SessionManager (mutex, storage.local)
  capture.ts                # NEW: content-script message → PageCapture event
  extract.ts                # NEW: Readability extraction (testable, DOM-dependent)
  content.ts                # NEW: thin content script (IIFE entry)
  popup/popup.html, popup/popup.ts   # NEW
extension/
  vite.content.config.ts    # NEW: second build pass for content.js
  manifest.base.json        # + action.default_popup, scripting permission, host_permissions
extension/tests/
  tabs.test.ts, ws.test.ts (hardening tests), sessions.test.ts,
  capture.test.ts, extract.test.ts (happy-dom)
```

Conventions unchanged (ms epoch UTC timestamps; imports never `../`, tests via `@src/*`; imperative commits, no AI attribution). Protocol stays v1 — new event types are additive; older backends reject them with error frames, which clients treat as skip (by design).

---

### Task 1: Backend — schema v2 (sessions, page_visits + FTS) and storage methods

**Files:**
- Modify: `backend/src/browserbuddy/db.py`
- Modify: `backend/tests/test_db.py`
- Modify: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/test_storage_sessions.py`

- [ ] **Step 1: Write failing migration test** — append to `backend/tests/test_db.py`:

```python
def test_v2_migration_upgrades_v1_data_intact(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://keep.me', 'Keep', 1000, 'import')"
    )
    conn.commit()
    conn.close()
    conn = db.connect(path)  # re-running migrations applies any new versions
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    tables = _tables(conn)
    assert {"sessions", "page_visits", "page_fts"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM history_entries").fetchone()[0] == 1
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_db.py -v -k v2`
Expected: FAIL — MAX(version) is 1; no sessions table.

- [ ] **Step 3: Append migration v2 to `MIGRATIONS` in `backend/src/browserbuddy/db.py`**

```python
    # v2: Tier-2 session capture — sessions and page visits (+FTS).
    """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        client_id TEXT NOT NULL,
        name TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        UNIQUE (client_id, started_at)
    );

    CREATE INDEX idx_sessions_active ON sessions (client_id) WHERE ended_at IS NULL;

    CREATE TABLE page_visits (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions (id),
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        visited_at INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        html_key TEXT,
        container TEXT,
        UNIQUE (session_id, url, visited_at)
    );

    CREATE VIRTUAL TABLE page_fts USING fts5(
        url, title, text,
        content='page_visits', content_rowid='id'
    );

    CREATE TRIGGER page_ai AFTER INSERT ON page_visits BEGIN
        INSERT INTO page_fts(rowid, url, title, text)
        VALUES (new.id, new.url, new.title, new.text);
    END;

    CREATE TRIGGER page_ad AFTER DELETE ON page_visits BEGIN
        INSERT INTO page_fts(page_fts, rowid, url, title, text)
        VALUES ('delete', old.id, old.url, old.title, old.text);
    END;

    CREATE TRIGGER page_au AFTER UPDATE ON page_visits BEGIN
        INSERT INTO page_fts(page_fts, rowid, url, title, text)
        VALUES ('delete', old.id, old.url, old.title, old.text);
        INSERT INTO page_fts(rowid, url, title, text)
        VALUES (new.id, new.url, new.title, new.text);
    END;
    """,
```

Run the migration test: expect PASS (and the whole suite still green — v1 assertions unchanged).

- [ ] **Step 4: Write failing storage tests** — `backend/tests/test_storage_sessions.py`:

```python
import pytest


def test_start_end_and_active_session(storage):
    sid = storage.start_session("ext-1", "quantum research", started_at=1000)
    active = storage.get_active_session("ext-1")
    assert active["id"] == sid
    assert active["name"] == "quantum research"
    assert storage.get_active_session("other-client") is None
    assert storage.end_session("ext-1", ended_at=2000) is True
    assert storage.get_active_session("ext-1") is None
    assert storage.end_session("ext-1", ended_at=3000) is False  # idempotent


def test_starting_a_session_ends_the_previous_one(storage):
    first = storage.start_session("ext-1", "one", started_at=1000)
    second = storage.start_session("ext-1", "two", started_at=2000)
    assert storage.get_active_session("ext-1")["id"] == second
    row = storage._conn.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (first,)
    ).fetchone()
    assert row["ended_at"] == 2000  # implicitly closed at the new start


def test_add_page_visit_and_search(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    n = storage.add_page_visit(
        sid,
        url="https://arxiv.org/abs/42",
        title="Sparse retrieval methods",
        visited_at=1500,
        text="A survey of sparse retrieval and ranking methods.",
        html_key="abc123",
        container="Research",
    )
    assert n == 1
    # duplicate (session, url, visited_at) ignored
    assert storage.add_page_visit(
        sid, url="https://arxiv.org/abs/42", title="x", visited_at=1500, text="y",
        html_key=None, container=None,
    ) == 0
    results = storage.search_pages("sparse ranking")
    assert len(results) == 1
    assert results[0]["url"] == "https://arxiv.org/abs/42"
    assert results[0]["session_name"] == "s"


def test_add_page_visit_requires_valid_session(storage):
    with pytest.raises(LookupError):
        storage.add_page_visit(
            9999, url="https://a.com", title="", visited_at=1, text="",
            html_key=None, container=None,
        )


def test_list_sessions_with_page_counts(storage):
    sid = storage.start_session("ext-1", "s1", started_at=1000)
    storage.add_page_visit(sid, url="https://a.com", title="", visited_at=1100,
                           text="", html_key=None, container=None)
    storage.end_session("ext-1", ended_at=2000)
    storage.start_session("ext-1", "s2", started_at=3000)
    rows = storage.list_sessions()
    assert [r["name"] for r in rows] == ["s2", "s1"]  # newest first
    assert rows[1]["page_count"] == 1
    assert rows[0]["ended_at"] is None
```

- [ ] **Step 5: Run to verify failure** — `uv run pytest tests/test_storage_sessions.py -v`: AttributeError (no start_session).

- [ ] **Step 6: Append to `Storage` in `backend/src/browserbuddy/storage.py`**

```python
    # -- sessions ----------------------------------------------------------

    def start_session(self, client_id: str, name: str, started_at: int) -> int:
        """Open a session; any still-active session for this client is
        implicitly closed at the new start time (one active session per
        client by design)."""
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE client_id = ? AND ended_at IS NULL",
                (started_at, client_id),
            )
            cur = self._conn.execute(
                "INSERT INTO sessions (client_id, name, started_at) VALUES (?, ?, ?)",
                (client_id, name, started_at),
            )
        return cur.lastrowid

    def end_session(self, client_id: str, ended_at: int) -> bool:
        """Close the active session. Returns False when none is active
        (idempotent — a replayed session_end is not an error)."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE client_id = ? AND ended_at IS NULL",
                (ended_at, client_id),
            )
        return cur.rowcount > 0

    def get_active_session(self, client_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE client_id = ? AND ended_at IS NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (client_id,),
        ).fetchone()

    def add_page_visit(
        self,
        session_id: int,
        *,
        url: str,
        title: str,
        visited_at: int,
        text: str,
        html_key: str | None,
        container: str | None,
    ) -> int:
        """Record a captured page. Returns rows inserted (0 on duplicate).

        Raises LookupError for an unknown session_id so the WS loop can send
        an error frame instead of dying.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            raise LookupError(f"no such session: {session_id}")
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO page_visits"
                " (session_id, url, title, visited_at, text, html_key, container)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, url, title, visited_at, text, html_key, container),
            )
        return cur.rowcount

    def list_sessions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT s.id, s.name, s.started_at, s.ended_at,
                   COUNT(p.id) AS page_count
            FROM sessions s LEFT JOIN page_visits p ON p.session_id = s.id
            GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def search_pages(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Full-text search over captured page text. Same OR + bm25 semantics
        as search_history (see that docstring)."""
        limit = max(1, min(limit, 200))
        tokens = query.split()
        if not tokens:
            return []
        fts_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        return self._conn.execute(
            """
            SELECT p.url, p.title, p.visited_at, s.name AS session_name
            FROM page_fts f
            JOIN page_visits p ON p.id = f.rowid
            JOIN sessions s ON s.id = p.session_id
            WHERE page_fts MATCH ?
            ORDER BY bm25(page_fts), p.visited_at DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
```

- [ ] **Step 7: Run** — `uv run pytest -q`: expect 60 passed (54 + 1 migration + 5 session). `uv run ruff check .` clean.

- [ ] **Step 8: Commit**

```bash
git add backend/src/browserbuddy/db.py backend/src/browserbuddy/storage.py \
    backend/tests/test_db.py backend/tests/test_storage_sessions.py
git commit -m "Add schema v2 with sessions and searchable page visits"
```

---

### Task 2: Backend — content-addressed blob store

**Files:**
- Create: `backend/src/browserbuddy/blobs.py`
- Create: `backend/tests/test_blobs.py`

- [ ] **Step 1: Write failing tests** — `backend/tests/test_blobs.py`:

```python
import gzip
import hashlib

import pytest

from browserbuddy.blobs import BlobStore


def test_put_get_roundtrip_and_key_is_sha256(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"<html><body>hello research</body></html>"
    key = store.put(data)
    assert key == hashlib.sha256(data).hexdigest()
    assert store.get(key) == data


def test_put_is_idempotent_and_deduplicates(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    key1 = store.put(b"same content")
    key2 = store.put(b"same content")
    assert key1 == key2
    files = list((tmp_path / "blobs").rglob("*.gz"))
    assert len(files) == 1


def test_blobs_are_gzipped_at_rest(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"x" * 100_000
    key = store.put(data)
    path = store.path_for(key)
    assert path.suffix == ".gz"
    assert path.stat().st_size < len(data) / 10
    assert gzip.decompress(path.read_bytes()) == data


def test_get_missing_raises(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    with pytest.raises(FileNotFoundError):
        store.get("0" * 64)
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Write `backend/src/browserbuddy/blobs.py`**

```python
"""Content-addressed blob storage for raw page HTML (and later screenshots).

Keys are the SHA-256 of the UNCOMPRESSED content; identical pages are stored
once. Files are gzipped at rest (raw HTML compresses ~10x). Layout:
<root>/<key[:2]>/<key[2:]>.gz — two-level fan-out keeps directories small.
"""

import gzip
import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self._root / key[:2] / f"{key[2:]}.gz"

    def put(self, data: bytes) -> str:
        """Store data, returning its content key. No-op if already stored."""
        key = hashlib.sha256(data).hexdigest()
        path = self.path_for(key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data))
            tmp.replace(path)  # atomic: readers never see partial writes
        return key

    def get(self, key: str) -> bytes:
        return gzip.decompress(self.path_for(key).read_bytes())
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_blobs.py -v`: 4 passed. Full suite: 64. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/blobs.py backend/tests/test_blobs.py
git commit -m "Add gzip content-addressed blob store"
```

---

### Task 3: Backend — session protocol events + ingest dispatch

**Files:**
- Modify: `backend/src/browserbuddy/models.py`
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_models.py`, `backend/tests/test_app.py`

- [ ] **Step 1: Write failing model tests** — append to `backend/tests/test_models.py`:

```python
def test_session_and_page_events_validate():
    env = EventEnvelope.model_validate(
        {"v": 1, "seq": 1, "event": {"type": "session_start", "name": "s", "started_at": 5}}
    )
    assert env.event.type == "session_start"
    env = EventEnvelope.model_validate(
        {"v": 1, "seq": 2, "event": {"type": "session_end", "ended_at": 9}}
    )
    assert env.event.type == "session_end"
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 3,
            "event": {
                "type": "page_capture",
                "url": "https://a.com",
                "visited_at": 7,
                "text": "body text",
                "html": "<html></html>",
            },
        }
    )
    assert env.event.type == "page_capture"
    assert env.event.title == ""
    assert env.event.container is None


def test_session_start_requires_nonempty_name():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {"v": 1, "seq": 1, "event": {"type": "session_start", "name": "", "started_at": 5}}
        )
```

- [ ] **Step 2: Add models to `backend/src/browserbuddy/models.py`** (after HistoryVisitsEvent):

```python
class SessionStartEvent(ProtocolModel):
    type: Literal["session_start"]
    name: str = Field(min_length=1)
    started_at: int = Field(ge=0)


class SessionEndEvent(ProtocolModel):
    type: Literal["session_end"]
    ended_at: int = Field(ge=0)


class PageCaptureEvent(ProtocolModel):
    type: Literal["page_capture"]
    url: str = Field(min_length=1)
    title: str = ""
    visited_at: int = Field(ge=0)
    text: str = ""
    html: str | None = None  # raw DOM HTML; stored content-addressed, not in SQLite
    container: str | None = None
```

and widen the envelope union:

```python
    event: (
        TabStateEvent | HistoryVisitsEvent | SessionStartEvent | SessionEndEvent | PageCaptureEvent
    ) = Field(discriminator="type")
```

Run the model tests (expect pass; whole file green).

- [ ] **Step 3: Write failing app tests** — append to `backend/tests/test_app.py`:

```python
def _envelope(seq, event):
    return {"v": 1, "seq": seq, "event": event}


def test_ws_session_lifecycle_and_page_capture(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "deep dive", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(2, {
                "type": "page_capture",
                "url": "https://arxiv.org/abs/7",
                "title": "Grounded decoding",
                "visited_at": 150,
                "text": "grounded decoding methods for agents",
                "html": "<html><body>grounded decoding</body></html>",
                "container": "Research",
            })
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(_envelope(3, {"type": "session_end", "ended_at": 200}))
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    storage = app.state.storage
    sessions = storage.list_sessions()
    assert sessions[0]["name"] == "deep dive"
    assert sessions[0]["page_count"] == 1
    results = storage.search_pages("grounded")
    assert results[0]["session_name"] == "deep dive"
    # html stored content-addressed
    row = storage._conn.execute("SELECT html_key FROM page_visits").fetchone()
    assert row["html_key"] is not None
    assert b"grounded decoding" in app.state.blobs.get(row["html_key"])


def test_ws_page_capture_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(1, {"type": "page_capture", "url": "https://a.com", "visited_at": 1})
        )
        reply = ws.receive_json()
        assert reply["type"] == "error"
        # connection survives; a session_start still works
        ws.send_json(_envelope(2, {"type": "session_start", "name": "s", "started_at": 2}))
        assert ws.receive_json() == {"type": "ack", "seq": 2}


def test_ws_session_end_without_session_is_idempotent_ack(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_end", "ended_at": 5}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
```

- [ ] **Step 4: Run to verify failure** — `uv run pytest tests/test_app.py -v -k "session or capture"`: the lifecycle test FAILS (page_capture/session events rejected as validation... no — models now accept them; _apply_event doesn't handle them → AssertionError from the defensive else). Confirm the actual failure mode and report it (it should be the `unhandled event type` assertion — exactly what that guard exists for).

- [ ] **Step 5: Wire dispatch in `backend/src/browserbuddy/app.py`**

- `create_app`: after storage/secret setup add `app.state.blobs = BlobStore(settings.data_dir / "blobs")` (import `from browserbuddy.blobs import BlobStore`).
- `_apply_event` gains the client identity and blob store — change its signature and the call site:

```python
def _apply_event(
    storage: Storage, blobs: BlobStore, client_id: str, envelope: EventEnvelope
) -> None:
    event = envelope.event
    if isinstance(event, TabStateEvent):
        storage.replace_open_tabs(event.tabs)
    elif isinstance(event, HistoryVisitsEvent):
        storage.insert_history_visits(event.visits, source="sync")
    elif isinstance(event, SessionStartEvent):
        storage.start_session(client_id, event.name, event.started_at)
    elif isinstance(event, SessionEndEvent):
        storage.end_session(client_id, event.ended_at)  # False = already ended; fine
    elif isinstance(event, PageCaptureEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise LookupError("page_capture with no active session")
        html_key = blobs.put(event.html.encode()) if event.html else None
        storage.add_page_visit(
            active["id"],
            url=event.url,
            title=event.title,
            visited_at=event.visited_at,
            text=event.text,
            html_key=html_key,
            container=event.container,
        )
    else:  # pragma: no cover - unreachable until the union grows
        raise AssertionError(f"unhandled event type: {type(event).__name__}")
```

In the WS loop, widen the apply-error handling to cover the new failure mode (same no-ack skip semantics as integrity errors):

```python
                    try:
                        _apply_event(storage, app.state.blobs, auth.client_id, envelope)
                        storage.set_last_seq(auth.client_id, envelope.seq)
                    except (sqlite3.IntegrityError, LookupError) as exc:
                        logger.warning("Rejected event seq=%d: %s", envelope.seq, exc)
                        await ws.send_json({"type": "error", "detail": str(exc)})
                        continue
```

(update imports for the new event classes and BlobStore).

- [ ] **Step 6: Run** — `uv run pytest -q`: expect 69 passed (64 + 2 model + 3 app). Ruff clean.

- [ ] **Step 7: Commit**

```bash
git add backend/src/browserbuddy/models.py backend/src/browserbuddy/app.py \
    backend/tests/test_models.py backend/tests/test_app.py
git commit -m "Add session and page-capture events to the ingest protocol"
```

---

### Task 4: Backend — CLI: `buddy sessions` + page search in `buddy search`

**Files:**
- Modify: `backend/src/browserbuddy/cli.py`
- Modify: `backend/tests/test_cli.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_cli.py`:

```python
def _seed_session(tmp_path):
    from browserbuddy import db
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    s = Storage(conn)
    sid = s.start_session("ext-1", "vector research", started_at=1720000000000)
    s.add_page_visit(
        sid, url="https://arxiv.org/abs/8", title="HNSW explained",
        visited_at=1720000001000, text="hierarchical navigable small worlds",
        html_key=None, container=None,
    )
    s.end_session("ext-1", ended_at=1720000100000)
    conn.close()


def test_sessions_lists_sessions(tmp_path):
    _seed_session(tmp_path)
    result = runner.invoke(app, ["sessions"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "vector research" in result.output
    assert "1 page" in result.output


def test_sessions_empty(tmp_path):
    result = runner.invoke(app, ["sessions"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_search_includes_session_pages(tmp_path):
    _seed_session(tmp_path)
    result = runner.invoke(app, ["search", "navigable"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "https://arxiv.org/abs/8" in result.output
    assert "vector research" in result.output
```

- [ ] **Step 2: Run to verify failure** — sessions command missing; search shows "No results."

- [ ] **Step 3: Extend `backend/src/browserbuddy/cli.py`**

Add a `sessions` command and extend `search` (keep the existing history block; connection handling stays try/finally):

```python
@app.command()
def sessions() -> None:
    """List research sessions (newest first)."""
    conn = db.connect(get_settings().db_path)
    try:
        rows = Storage(conn).list_sessions()
    finally:
        conn.close()
    if not rows:
        typer.echo("No sessions recorded yet.")
        return
    for row in rows:
        state = "active" if row["ended_at"] is None else _fmt_time(row["ended_at"])
        pages = f"{row['page_count']} page" + ("" if row["page_count"] == 1 else "s")
        typer.echo(f"{_fmt_time(row['started_at'])} → {state}  {row['name']}  ({pages})")
```

In `search`, after printing history results (or "No results." handling — restructure minimally):

```python
@app.command()
def search(
    query: str,
    limit: int = typer.Option(20, help="Maximum results (capped at 200)."),
) -> None:
    """Full-text search over history and captured session pages."""
    conn = db.connect(get_settings().db_path)
    try:
        storage = Storage(conn)
        history = storage.search_history(query, limit=limit)
        pages = storage.search_pages(query, limit=limit)
    finally:
        conn.close()
    if not history and not pages:
        typer.echo("No results.")
        return
    for row in history:
        typer.echo(f"{_fmt_time(row['visit_time'])}  {row['title'] or '(untitled)'}")
        typer.echo(f"                  {row['url']}")
    if pages:
        typer.echo("-- session pages --")
        for row in pages:
            typer.echo(
                f"{_fmt_time(row['visited_at'])}  {row['title'] or '(untitled)'}"
                f"  [{row['session_name']}]"
            )
            typer.echo(f"                  {row['url']}")
```

- [ ] **Step 4: Run** — `uv run pytest -q`: expect 72 passed. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/cli.py backend/tests/test_cli.py
git commit -m "Add sessions command and include page captures in search"
```

---

### Task 5: Extension — M2 follow-up hardenings

**Files:**
- Modify: `extension/src/tabs.ts`, `extension/src/ws.ts`, `extension/src/background.ts`
- Modify: `extension/tests/tabs.test.ts`, `extension/tests/ws.test.ts`

These are the recorded M3 follow-ups from the M2 final review (plan `2026-07-16-extension-tier1.md`, follow-ups section, item 5).

- [ ] **Step 1: Failing test — active_ms clamp** (append to `extension/tests/tabs.test.ts`):

```ts
test("wall-clock regression cannot bank negative active time", async () => {
  let t = 1_000_000;
  const tracker = new TabTracker(fakeStorageArea(), () => t);
  await tracker.recordActivation(1, 1);
  t -= 5000; // clock went backwards (NTP correction)
  await tracker.recordFocusLost(); // banks now() - activeSince = -5000 without a clamp
  const records = await tracker.snapshot([tab(1, "https://a.com")], noContainer);
  expect(records[0]!.active_ms).toBe(0); // clamped, not negative (backend rejects ge<0)
});
```

Run: FAILS (active_ms is -5000).

- [ ] **Step 2: Fix in `extension/src/tabs.ts`** — clamp both accounting points:
  - in `bank()`: `this.ensure(state, state.activeTabId).active_ms += Math.max(0, this.now() - state.activeSince);`
  - in `snapshotImpl()`'s in-flight interval: `activeMs += Math.max(0, this.now() - state.activeSince);`
  - comment on either: `// Clamp: a wall-clock regression must not produce negative active_ms — the backend's ge=0 validation would reject every subsequent tab_state containing this tab.`

- [ ] **Step 3: Failing test — teardown nulls all handlers** (append to `extension/tests/ws.test.ts`):

```ts
test("frames delivered after teardown do not touch successor state", async () => {
  const { sockets, queue, client } = setup();
  client.connect();
  const s0 = sockets[0]!;
  s0.open();
  s0.receive({ type: "auth_ok", last_seq: 0 });
  await vi.advanceTimersByTimeAsync(0);
  client.disconnect(); // teardown must detach s0's handlers entirely
  expect(s0.onmessage).toBeNull();
  expect(s0.onopen).toBeNull();
  await queue.enqueue(visits("https://a.com"));
  s0.receive({ type: "ack", seq: 1 }); // stray late frame: must be a no-op
  await vi.advanceTimersByTimeAsync(0);
  expect(await queue.pending()).toHaveLength(1); // not acked by a dead socket
});
```

Run: FAILS (onmessage still wired; stray ack prunes the queue).

- [ ] **Step 4: Fix in `extension/src/ws.ts` `teardownSocket`** — null all three handlers before close:

```ts
      this.socket.onclose = null; // avoid re-entrant handleClose from close()
      this.socket.onmessage = null; // late frames must not touch successor state
      this.socket.onopen = null;
```

- [ ] **Step 5: background.ts — wrap the remaining unlogged chains** (no unit test; wiring):
  - `onFocusChanged` listener: wrap both promise chains in `logged(...)`
  - the `alarms.get("bb-sync").then(...)` chain: wrap in `logged(...)`

- [ ] **Step 6: Run** — `npm run check`: 35 tests, clean. Commit:

```bash
git add extension/src/tabs.ts extension/src/ws.ts extension/src/background.ts \
    extension/tests/tabs.test.ts extension/tests/ws.test.ts
git commit -m "Clamp active-time banking and detach handlers on socket teardown"
```

---

### Task 6: Extension — SessionManager

**Files:**
- Create: `extension/src/sessions.ts`
- Create: `extension/tests/sessions.test.ts`

- [ ] **Step 1: Write failing tests** — `extension/tests/sessions.test.ts`:

```ts
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
```

- [ ] **Step 2: Run to verify failure** — cannot resolve `@src/sessions`.

- [ ] **Step 3: Write `extension/src/sessions.ts`**

```ts
import type { EventPayload, StorageArea } from "./types";

export interface SessionState {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}

const KEY = "bb:session";

/** Explicit research-session state. Persisted in storage.local so a session
 * survives worker death AND browser restart (deliberate: the user said
 * "record" — only the user saying stop ends it). Pause/resume are
 * client-side only: a paused session sends no page captures; the backend
 * session stays open. Mutations are mutex-serialized like the other
 * storage-backed modules. */
export class SessionManager {
  private mutex: Promise<unknown> = Promise.resolve();

  constructor(
    private local: StorageArea,
    private now: () => number,
  ) {}

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.mutex.then(fn, fn);
    this.mutex = next.catch(() => undefined);
    return next;
  }

  async current(): Promise<SessionState | null> {
    const stored = (await this.local.get(KEY))[KEY] as SessionState | undefined;
    return stored ?? null;
  }

  start(name: string): Promise<{ event: EventPayload }> {
    return this.serialize(async () => {
      const startedAt = this.now();
      const state: SessionState = { name, startedAt, status: "recording" };
      await this.local.set({ [KEY]: state });
      // Starting over an active session is fine: the backend implicitly
      // closes the previous session at the new start time.
      return { event: { type: "session_start", name, started_at: startedAt } };
    });
  }

  pause(): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state?.status === "recording") {
        await this.local.set({ [KEY]: { ...state, status: "paused" } });
      }
    });
  }

  resume(): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state?.status === "paused") {
        await this.local.set({ [KEY]: { ...state, status: "recording" } });
      }
    });
  }

  stop(): Promise<{ event: EventPayload } | null> {
    return this.serialize(async () => {
      const state = await this.current();
      if (!state) return null;
      await this.local.remove(KEY);
      return { event: { type: "session_end", ended_at: this.now() } };
    });
  }
}
```

Note: this requires the `EventPayload` union to include the new event types — that lands in Task 7's types change. To keep THIS task self-contained and green, Task 6 and Task 7's types edit may be done in either order; if tsc complains here, do Task 7 Step 1 (types.ts) first and note it in the report.

- [ ] **Step 4: Run** — `npm run check`: 40 tests, clean (after the types union exists). Commit:

```bash
git add extension/src/sessions.ts extension/tests/sessions.test.ts
git commit -m "Add mutex-serialized session manager"
```

---

### Task 7: Extension — protocol types + capture module

**Files:**
- Modify: `extension/src/types.ts`
- Create: `extension/src/capture.ts`
- Create: `extension/tests/capture.test.ts`

- [ ] **Step 1: Extend `extension/src/types.ts`** — mirror the backend additions exactly:

```ts
export interface PageCapture {
  url: string;
  title: string;
  visited_at: number;
  text: string;
  html: string | null;
  container: string | null;
}

export type EventPayload =
  | { type: "tab_state"; tabs: TabRecord[] }
  | { type: "history_visits"; visits: HistoryVisit[] }
  | { type: "session_start"; name: string; started_at: number }
  | { type: "session_end"; ended_at: number }
  | ({ type: "page_capture" } & PageCapture);
```

- [ ] **Step 2: Write failing tests** — `extension/tests/capture.test.ts`:

```ts
import { expect, test } from "vitest";
import { MAX_HTML_CHARS, pageCaptureEvent } from "@src/capture";

const msg = (over: Record<string, unknown> = {}) => ({
  type: "bb:page",
  url: "https://a.com/article",
  title: "An article",
  text: "body text",
  html: "<html><body>body text</body></html>",
  ...over,
});

test("converts a content-script message to a page_capture event", () => {
  const event = pageCaptureEvent(msg(), "Research", () => 1_000_000);
  expect(event).toEqual({
    type: "page_capture",
    url: "https://a.com/article",
    title: "An article",
    visited_at: 1_000_000,
    text: "body text",
    html: "<html><body>body text</body></html>",
    container: "Research",
  });
});

test("rejects messages without a usable url or text", () => {
  expect(pageCaptureEvent(msg({ url: "" }), null, () => 1)).toBeNull();
  expect(pageCaptureEvent(msg({ url: undefined }), null, () => 1)).toBeNull();
  expect(pageCaptureEvent(msg({ text: "", html: "" }), null, () => 1)).toBeNull();
});

test("oversized html is dropped but the text capture survives", () => {
  const event = pageCaptureEvent(
    msg({ html: "x".repeat(MAX_HTML_CHARS + 1) }),
    null,
    () => 1,
  );
  expect(event).not.toBeNull();
  expect(event!.html).toBeNull();
  expect(event!.text).toBe("body text");
});

test("defaults: missing title becomes empty string", () => {
  const event = pageCaptureEvent(msg({ title: undefined }), null, () => 1);
  expect(event!.title).toBe("");
});
```

- [ ] **Step 3: Run to verify failure**, then write `extension/src/capture.ts`:

```ts
import type { EventPayload } from "./types";

/** Cap on inline raw HTML. Larger pages keep their extracted text; the raw
 * DOM is dropped (bounds WS frame size — the server caps frames at 16 MB,
 * and multi-MB outliers are rarely worth archiving). */
export const MAX_HTML_CHARS = 2_000_000;

export interface PageMessage {
  type?: string;
  url?: string;
  title?: string;
  text?: string;
  html?: string;
}

/** Convert a content-script "bb:page" message into a page_capture event.
 * Returns null for unusable messages (no url, or nothing captured). */
export function pageCaptureEvent(
  message: PageMessage,
  container: string | null,
  now: () => number = Date.now,
): (EventPayload & { type: "page_capture" }) | null {
  if (!message.url) return null;
  const text = message.text ?? "";
  let html = message.html ?? null;
  if (!text && !html) return null;
  if (html !== null && html.length > MAX_HTML_CHARS) {
    console.warn("BrowserBuddy: raw HTML too large, keeping text only", message.url);
    html = null;
  }
  return {
    type: "page_capture",
    url: message.url,
    title: message.title ?? "",
    visited_at: now(),
    text,
    html,
    container,
  };
}
```

- [ ] **Step 4: Run** — `npm run check`: 44 tests, clean. Commit:

```bash
git add extension/src/types.ts extension/src/capture.ts extension/tests/capture.test.ts
git commit -m "Add page-capture event types and message conversion"
```

---

### Task 8: Extension — extraction module + content script + content build

**Files:**
- Create: `extension/src/extract.ts`, `extension/src/content.ts`
- Create: `extension/vite.content.config.ts`
- Create: `extension/tests/extract.test.ts`
- Modify: `extension/package.json` (deps + build script)

- [ ] **Step 1: Install deps**

```bash
cd /Users/david/Projects/BrowserBuddy/extension
npm install @mozilla/readability
npm install -D happy-dom
```

(@mozilla/readability is a runtime dependency — the spec's extraction engine, same one Firefox reader mode uses. happy-dom provides a DOM for the extraction tests.)

- [ ] **Step 2: Write failing tests** — `extension/tests/extract.test.ts`:

```ts
// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { extractPage } from "@src/extract";

function loadDocument(html: string, url = "https://example.com/post"): Document {
  const doc = document.implementation.createHTMLDocument();
  doc.documentElement.innerHTML = html;
  // Readability reads the document URI for relative links; happy-dom lets us fake it.
  Object.defineProperty(doc, "URL", { value: url });
  Object.defineProperty(doc, "documentURI", { value: url });
  return doc;
}

const ARTICLE = `
  <head><title>Test Article</title></head>
  <body>
    <nav>Home | About | Contact</nav>
    <article>
      <h1>Understanding Vector Databases</h1>
      ${"<p>Vector databases store embeddings for similarity search. This paragraph repeats to give Readability enough content to work with confidently.</p>".repeat(8)}
    </article>
    <footer>Copyright</footer>
  </body>`;

test("extracts readable text and raw html from an article page", () => {
  const result = extractPage(loadDocument(ARTICLE));
  expect(result.title).toBe("Test Article");
  expect(result.text).toContain("similarity search");
  expect(result.text).not.toContain("Home | About"); // nav stripped by Readability
  expect(result.html).toContain("<article>"); // raw DOM, not the readable version
});

test("falls back to body text when Readability finds no article", () => {
  const result = extractPage(loadDocument("<body><div>just a fragment</div></body>"));
  expect(result.text).toContain("just a fragment");
  expect(result.html).toContain("just a fragment");
});

test("never throws on a pathological document", () => {
  const result = extractPage(loadDocument("<body></body>"));
  expect(result.text).toBe("");
  expect(typeof result.html).toBe("string");
});
```

- [ ] **Step 3: Run to verify failure**, then write `extension/src/extract.ts`:

```ts
import { Readability } from "@mozilla/readability";

export interface ExtractedPage {
  title: string;
  text: string;
  html: string;
}

/** Extract readable text + raw DOM HTML from a live document.
 *
 * Readability mutates its input, so it runs on a clone. The raw HTML is the
 * RENDERED DOM (post-JS), which is the point of extracting in the content
 * script: the backend can re-extract later, but only from what the page
 * actually showed (spec: raw HTML stored content-addressed for
 * reprocessability). Never throws — a capture failure must not break pages.
 */
export function extractPage(doc: Document): ExtractedPage {
  const html = doc.documentElement?.outerHTML ?? "";
  const title = doc.title ?? "";
  let text = "";
  try {
    const article = new Readability(doc.cloneNode(true) as Document).parse();
    text = article?.textContent?.trim() ?? "";
  } catch {
    text = "";
  }
  if (!text) {
    text = doc.body?.textContent?.replace(/\s+/g, " ").trim() ?? "";
  }
  return { title, text, html };
}
```

- [ ] **Step 4: Write `extension/src/content.ts`** (thin IIFE entry — no exports, no module imports at runtime beyond the bundle):

```ts
import { extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts once the page
// settles and reports home. SPA recapture lands in M3b.
(() => {
  const send = () => {
    const page = extractPage(document);
    void (globalThis as { browser?: typeof browser }).browser
      ?.runtime.sendMessage({ type: "bb:page", url: location.href, ...page })
      .catch(() => undefined); // background asleep/unreachable: drop silently
  };
  if (document.readyState === "complete") {
    send();
  } else {
    addEventListener("load", () => setTimeout(send, 500), { once: true });
  }
})();
```

(Note: Firefox content scripts always have `browser`; the defensive access keeps Chrome from throwing when this ships there — Chrome content scripts get `chrome`, which the M3b port will alias.)

- [ ] **Step 5: Write `extension/vite.content.config.ts`** (content scripts cannot be ES modules — IIFE second pass):

```ts
import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: { alias: { "@src": resolve(import.meta.dirname, "src") } },
  build: {
    outDir: "dist/assets",
    emptyOutDir: false, // main pass already populated it
    lib: {
      entry: resolve(import.meta.dirname, "src/content.ts"),
      formats: ["iife"],
      name: "bbcontent",
      fileName: () => "content.js",
    },
    target: "es2022",
  },
});
```

and chain it in `extension/package.json`:

```json
    "build": "vite build && vite build -c vite.content.config.ts && node scripts/emit-manifests.mjs",
```

- [ ] **Step 6: Run** — `npm run check` (47 tests, clean) and `npm run build`; confirm `dist/firefox/content.js` exists and is an IIFE (starts with `(function` or `var bbcontent`), NOT an import statement.

- [ ] **Step 7: Commit**

```bash
git add extension/src/extract.ts extension/src/content.ts extension/vite.content.config.ts \
    extension/tests/extract.test.ts extension/package.json extension/package-lock.json
git commit -m "Add Readability extraction and session content script"
```

---

### Task 9: Extension — manifest + background session wiring

**Files:**
- Modify: `extension/manifest.base.json`, `extension/manifest.firefox.json`, `extension/manifest.chrome.json`
- Modify: `extension/vite.config.ts` (popup input)
- Modify: `extension/src/background.ts`
- Create: `extension/src/popup/popup.html` (placeholder so the build has the entry; real UI in Task 10)

- [ ] **Step 1: Manifest changes**

`manifest.base.json` gains:

```json
  "action": { "default_popup": "src/popup/popup.html", "default_title": "BrowserBuddy" },
  "host_permissions": ["<all_urls>"]
```

Both overlays' `permissions` arrays gain `"scripting"` (Firefox: 7 entries; Chrome: 5). Note: in Firefox MV3 host permissions are NOT granted at install — the popup requests them on the Start gesture (Task 10); in Chrome they prompt at install. This is by design.

`vite.config.ts` rollup inputs gain:

```ts
        popup: resolve(import.meta.dirname, "src/popup/popup.html"),
```

Placeholder `extension/src/popup/popup.html` (replaced in Task 10):

```html
<!doctype html>
<html><head><meta charset="utf-8" /><title>BrowserBuddy</title></head>
<body><p>Popup UI in Task 10.</p></body></html>
```

- [ ] **Step 2: Extend `extension/src/background.ts`** — add imports:

```ts
import { pageCaptureEvent, type PageMessage } from "./capture";
import { SessionManager } from "./sessions";
```

module-level instance (next to queue/tracker):

```ts
const sessionManager = new SessionManager(local, Date.now);
```

session plumbing (new functions, placed before the listeners):

```ts
const CAPTURE_SCRIPT_ID = "bb-capture";

async function registerCapture(): Promise<void> {
  const existing = await browser.scripting.getRegisteredContentScripts({
    ids: [CAPTURE_SCRIPT_ID],
  });
  if (existing.length === 0) {
    await browser.scripting.registerContentScripts([
      {
        id: CAPTURE_SCRIPT_ID,
        js: ["content.js"],
        matches: ["<all_urls>"],
        runAt: "document_idle",
        persistAcrossSessions: true,
      },
    ]);
  }
}

async function unregisterCapture(): Promise<void> {
  try {
    await browser.scripting.unregisterContentScripts({ ids: [CAPTURE_SCRIPT_ID] });
  } catch {
    // not registered — fine
  }
}

function setBadge(status: "recording" | "paused" | null): void {
  const text = status === "recording" ? "REC" : status === "paused" ? "II" : "";
  void browser.action.setBadgeText({ text });
  void browser.action.setBadgeBackgroundColor({
    color: status === "recording" ? "#c0392b" : "#7f8c8d",
  });
}

interface SessionCommand {
  type: "bb:session";
  action: "start" | "pause" | "resume" | "stop" | "status";
  name?: string;
}

async function handleSessionCommand(
  cmd: SessionCommand,
): Promise<{ session: unknown; connection: string }> {
  switch (cmd.action) {
    case "start": {
      const { event } = await sessionManager.start(cmd.name ?? "untitled session");
      await queue.enqueue(event);
      await (await ensureClient()).notifyEnqueued();
      await registerCapture();
      setBadge("recording");
      break;
    }
    case "pause":
      await sessionManager.pause();
      await unregisterCapture(); // paused = capture fully off
      setBadge("paused");
      break;
    case "resume":
      await sessionManager.resume();
      await registerCapture();
      setBadge("recording");
      break;
    case "stop": {
      const result = await sessionManager.stop();
      await unregisterCapture();
      setBadge(null);
      if (result) {
        await queue.enqueue(result.event);
        await (await ensureClient()).notifyEnqueued();
      }
      break;
    }
    case "status":
      break;
  }
  return { session: await sessionManager.current(), connection: clientStatus };
}

async function handlePageMessage(
  msg: PageMessage,
  cookieStoreId: string | undefined,
): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status !== "recording") return; // stale content script or paused
  const container = await containerName(cookieStoreId);
  const event = pageCaptureEvent(msg, container);
  if (!event) return;
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
}
```

replace the existing `runtime.onMessage` listener with:

```ts
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message === "bb:status") {
    sendResponse({ status: clientStatus });
    return true;
  }
  const msg = message as { type?: string };
  if (msg?.type === "bb:page") {
    logged(handlePageMessage(msg as PageMessage, sender.tab?.cookieStoreId));
    return false; // no reply — content script fire-and-forget
  }
  if (msg?.type === "bb:session") {
    void handleSessionCommand(msg as SessionCommand).then(sendResponse);
    return true; // async reply
  }
  return false;
});
```

and reconcile on every worker wake (bottom of file, next to the initial ensureClient):

```ts
// Reconcile capture registration with persisted session state on wake:
// a browser restart keeps the session recording (user never said stop).
logged(
  (async () => {
    const state = await sessionManager.current();
    if (state?.status === "recording") {
      await registerCapture();
    } else {
      await unregisterCapture();
    }
    setBadge(state?.status ?? null);
  })(),
);
```

- [ ] **Step 3: Run** — `npm run check` (47 tests, clean) and `npm run build`; inspect `dist/firefox/manifest.json` (action, host_permissions, scripting permission present; popup asset emitted at `src/popup/popup.html`; `content.js` at root).

- [ ] **Step 4: Commit**

```bash
git add extension/manifest.base.json extension/manifest.firefox.json extension/manifest.chrome.json \
    extension/vite.config.ts extension/src/background.ts extension/src/popup/popup.html
git commit -m "Wire session lifecycle: dynamic capture registration and badge"
```

---

### Task 10: Extension — popup UI

**Files:**
- Modify: `extension/src/popup/popup.html`
- Create: `extension/src/popup/popup.ts`

- [ ] **Step 1: Replace `extension/src/popup/popup.html`**

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>BrowserBuddy</title>
    <style>
      body { font: 13px system-ui; width: 280px; padding: 0.75rem; }
      h1 { font-size: 14px; margin: 0 0 0.5rem; }
      input { width: 100%; padding: 0.35rem; box-sizing: border-box; }
      button { margin: 0.5rem 0.25rem 0 0; padding: 0.3rem 0.9rem; }
      #session { margin: 0.5rem 0; font-weight: 600; }
      .recording { color: #c0392b; }
      .paused { color: #7f8c8d; }
      #connection { color: #555; margin-top: 0.5rem; font-size: 12px; }
      .hidden { display: none; }
    </style>
  </head>
  <body>
    <h1>BrowserBuddy</h1>
    <div id="idle">
      <input id="name" type="text" placeholder="Session name (what are you researching?)" />
      <button id="start">Start session</button>
    </div>
    <div id="active" class="hidden">
      <div id="session"></div>
      <button id="pause">Pause</button>
      <button id="resume" class="hidden">Resume</button>
      <button id="stop">Stop</button>
    </div>
    <div id="connection">…</div>
    <script type="module" src="./popup.ts"></script>
  </body>
</html>
```

- [ ] **Step 2: Write `extension/src/popup/popup.ts`**

```ts
interface SessionView {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}
interface SessionReply {
  session: SessionView | null;
  connection: string;
}

const el = <T extends HTMLElement>(id: string): T =>
  document.querySelector<T>(`#${id}`)!;

const idle = el<HTMLDivElement>("idle");
const active = el<HTMLDivElement>("active");
const nameInput = el<HTMLInputElement>("name");
const sessionEl = el<HTMLDivElement>("session");
const pauseBtn = el<HTMLButtonElement>("pause");
const resumeBtn = el<HTMLButtonElement>("resume");
const connectionEl = el<HTMLDivElement>("connection");

function render(reply: SessionReply): void {
  const s = reply.session;
  idle.classList.toggle("hidden", s !== null);
  active.classList.toggle("hidden", s === null);
  if (s) {
    sessionEl.textContent =
      s.status === "recording" ? `● Recording: ${s.name}` : `❚❚ Paused: ${s.name}`;
    sessionEl.className = s.status;
    pauseBtn.classList.toggle("hidden", s.status !== "recording");
    resumeBtn.classList.toggle("hidden", s.status !== "paused");
  }
  connectionEl.textContent = `Backend: ${reply.connection}`;
}

async function command(action: string, name?: string): Promise<void> {
  const reply = (await browser.runtime.sendMessage({
    type: "bb:session",
    action,
    name,
  })) as SessionReply;
  render(reply);
}

el<HTMLButtonElement>("start").addEventListener("click", () => {
  void (async () => {
    // Firefox MV3: host permissions are optional; capture needs them. Must
    // be requested from this user gesture — the background can't ask.
    const granted = await browser.permissions.request({ origins: ["<all_urls>"] });
    if (!granted) {
      connectionEl.textContent = "Capture needs page access to record sessions.";
      return;
    }
    await command("start", nameInput.value.trim() || "untitled session");
  })();
});
el<HTMLButtonElement>("pause").addEventListener("click", () => void command("pause"));
el<HTMLButtonElement>("resume").addEventListener("click", () => void command("resume"));
el<HTMLButtonElement>("stop").addEventListener("click", () => void command("stop"));

void command("status");
setInterval(() => void command("status"), 2000);
```

- [ ] **Step 3: Run** — `npm run check` (clean) and `npm run build` (popup assets emitted; `dist/firefox/src/popup/popup.html` references built JS).

- [ ] **Step 4: Commit**

```bash
git add extension/src/popup
git commit -m "Add popup UI for session start, pause, resume, and stop"
```

---

### Task 11: Gates, automated e2e, smoke instructions, README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Full gates, both projects**

```bash
cd /Users/david/Projects/BrowserBuddy/extension && npm run check && npm run build && npx web-ext lint --source-dir dist/firefox
cd /Users/david/Projects/BrowserBuddy/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: extension clean (47 tests; lint 0 errors — the pre-existing data-collection warning plus possibly a host_permissions notice are acceptable, report them), backend clean (72 tests).

- [ ] **Step 2: Automated protocol e2e** (same pattern as M2 Task 11): scratch Node WS client against a temp-data-dir `buddy serve` on port 8899 — auth, then `session_start` (name "e2e session"), `page_capture` (with html), `session_end`, expecting acks; then `page_capture` again expecting an ERROR frame (no active session). Verify via `BROWSERBUDDY_DATA_DIR=... uv run buddy sessions` (shows "e2e session", 1 page) and `buddy search` (finds the page text with the session name). Also verify a blob landed: `fd . /tmp/<datadir>/blobs -t f` shows one `.gz`. Kill server, clean up. Report verbatim outputs.

- [ ] **Step 3: Update `README.md`** — in the extension section, after the pairing paragraph, add:

```markdown
### Research sessions

Click the BrowserBuddy toolbar icon → name your session → **Start session**
(Firefox will ask for page access — that grant is what turns capture on).
Pages you read are captured (readable text + raw HTML) until you **Stop**.
Review from the terminal: `buddy sessions`, `buddy search "<anything you read>"`.
```

- [ ] **Step 4: Commit**

```bash
cd /Users/david/Projects/BrowserBuddy
git add README.md
git commit -m "Document research sessions"
```

- [ ] **Step 5: Hand off the interactive smoke to the user** (not automatable): serve + `npm run smoke`, pair, start a named session from the popup (grant the permission prompt), browse 2–3 article pages, `buddy sessions` + `buddy search`, pause/resume behavior, stop, confirm badge transitions (REC / II / cleared) and that a paused session captures nothing.

---

## Self-review notes

- **Spec coverage (M3a):** explicit sessions with popup start/pause/stop + recording badge ✓; Readability text + raw rendered HTML captured, HTML content-addressed + gzipped ✓ (spec's "screenshots and raw page HTML as content-addressed blobs"); one-active-session-per-client with implicit close ✓; sessions/pages searchable (FTS5, OR+bm25 consistent with history) ✓; content script only injected during sessions (dynamic registration, unregistered on pause/stop) ✓; host permissions requested on the Start gesture (Firefox MV3 optional-host-permissions reality) ✓. Deliberately deferred: links/dwell/nav-edges/SPA-recapture/screenshots (M3b), sensitive-content firewall (roadmap), digests/agent/embeddings for page text (M4 — page_fts serves keyword search until then).
- **Protocol:** additive event types under v1; old backends error-frame unknown types and clients skip — consistent with documented error semantics. page_capture with no active session → LookupError → error frame, no ack, connection survives (mirrors IntegrityError path).
- **Type consistency:** PageCaptureEvent (backend) ↔ PageCapture + EventPayload arm (extension) field-for-field; SessionManager events match SessionStartEvent/SessionEndEvent; capture.ts MAX_HTML_CHARS bounds the 16MB frame limit with big margin; test counts accumulate 60→64→69→72 (backend), 35→40→44→47 (extension).
- **Known simplifications (deliberate):** raw HTML travels inline in the WS event rather than the spec's REST-for-blobs (that endpoint arrives with screenshots in M3b; inline is within frame limits at the 2MB cap and content-addressing dedupes storage); pause is client-side only (backend session stays open); popup polls at 2s like the options page.


## Follow-ups for M3b

Recorded from the final whole-branch review (2026-07-20); none block this
milestone:

1. **Dangling backend session**: if `sessionManager.stop()` succeeds but the
   `session_end` enqueue throws, the backend session stays open until the
   next `session_start` implicitly closes it. The M4 digest generator must
   tolerate open-ended sessions (or M3b adds a reconcile).
2. **Fast stop→start mislabel residuals** (≤500ms window, benign): a stale
   content script's settle timer can attribute the old page to the new
   session; a capture interleaving inside the start command can land in the
   old still-open session. Accepted; revisit if digests show mislabels.
3. **Chrome alias gap in popup.ts** (bare `browser`, like options.ts — M2
   follow-up §4): dist/chrome popup would throw. Part of the deferred
   Chrome port.
4. **web-ext lint warnings** (zero-warnings justification): 2×
   `UNSAFE_VAR_ASSIGNMENT` come from bundled @mozilla/readability internals
   (innerHTML use inside the library) — not our code, not fixable without
   forking; plus the pre-existing AMO `data_collection_permissions` item.
5. **Carried M2 leftovers, now more pressing**: EventQueue rewrites the whole
   array per enqueue/ack and captures now carry up to 2MB html — chunked
   storage keys or per-event keys before M3b's screenshot volume; idle-API
   integration for active-time honesty. Also: backend `gzip.compress` of
   large html runs on the event loop (move to a thread with M3b's REST blob
   endpoint); `background.ts` reply-state read outside the lifecycle mutex
   (popup self-corrects via poll); CLI search header asymmetry (cosmetic).
