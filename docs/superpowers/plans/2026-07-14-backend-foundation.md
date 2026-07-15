# BrowserBuddy Backend Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working local backend: FastAPI server with authenticated WebSocket ingest (protocol v1), SQLite storage with FTS5 history search, Firefox `places.sqlite` backfill importer, and a `buddy` CLI that can import and search browser history and show open tabs.

**Architecture:** Single SQLite database (WAL) behind a synchronous `Storage` class; FastAPI app factory wires storage + shared-secret auth; the WebSocket ingest endpoint validates versioned, sequence-numbered event envelopes (Pydantic discriminated union) and acks each one, skipping already-seen sequence numbers for idempotency. The importer reads a *copy* of Firefox's `places.sqlite`. The CLI (Typer) drives serve/import/search/tabs.

**Tech Stack:** Python 3.12+, uv, FastAPI, uvicorn, Pydantic v2 + pydantic-settings, SQLite (stdlib `sqlite3`, FTS5), Typer, pytest + FastAPI TestClient, ruff.

**Spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`

**This is Plan 1 of ~6.** Later milestones (extension Tier 1, extension Tier 2 session capture, recall agent + MCP, digests, dashboard) get their own plans once this ships. This plan covers only Tier-1 *storage/ingest/search* — no LLM, no embeddings, no extension code.

---

## File structure

```
backend/
  pyproject.toml                  # uv project, deps, ruff config, buddy entry point
  src/browserbuddy/
    __init__.py
    config.py                     # Settings (data dir, host, port) via env BROWSERBUDDY_*
    db.py                         # connect(), migration runner, schema v1
    models.py                     # ingest protocol v1: envelopes + payload models
    auth.py                       # shared secret create/verify
    storage.py                    # all DB operations (history, tabs, client seq)
    app.py                        # FastAPI app factory: /health, /ws ingest
    cli.py                        # Typer CLI: serve, secret, import-history, search, tabs
    importer/
      __init__.py
      firefox.py                  # profiles.ini discovery + places.sqlite import
  tests/
    conftest.py                   # storage + app/client fixtures
    test_config.py
    test_db.py
    test_models.py
    test_auth.py
    test_storage_history.py
    test_storage_tabs.py
    test_app.py                   # /health + WS auth/ack/idempotency
    test_importer_firefox.py
    test_cli.py
```

Conventions used throughout: timestamps are **integer milliseconds since epoch, UTC**. All code lives under `backend/`; run all commands from `backend/`.

---

### Task 1: Project scaffold

**Files:**
- Create: `backend/pyproject.toml` (via `uv init` + edits)
- Create: `backend/src/browserbuddy/__init__.py`
- Create: `backend/tests/test_config.py` (placeholder import test)

- [ ] **Step 1: Scaffold the uv project**

```bash
cd /Users/david/Projects/BrowserBuddy
mkdir -p backend
cd backend
uv init --lib --name browserbuddy --python 3.12
rm -f src/browserbuddy/py.typed  # keep if present, harmless either way
uv add fastapi "uvicorn[standard]" pydantic-settings typer
uv add --dev pytest httpx ruff
```

`uv add` resolves current stable versions (satisfies the "look up current versions" rule).

- [ ] **Step 2: Configure ruff and pytest in `backend/pyproject.toml`**

Append to `pyproject.toml`:

```toml
[project.scripts]
buddy = "browserbuddy.cli:app"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Write a smoke test**

`backend/tests/test_config.py`:

```python
def test_package_imports():
    import browserbuddy  # noqa: F401
```

- [ ] **Step 4: Run it**

Run: `uv run pytest -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/david/Projects/BrowserBuddy
printf '.venv/\n__pycache__/\n*.egg-info/\n.pytest_cache/\n.ruff_cache/\n' > .gitignore
git add .gitignore backend
git commit -m "Scaffold backend uv project"
```

---

### Task 2: Config module

**Files:**
- Create: `backend/src/browserbuddy/config.py`
- Modify: `backend/tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Replace `backend/tests/test_config.py`:

```python
from browserbuddy.config import Settings, get_settings


def test_defaults():
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.data_dir.name == ".browserbuddy"
    assert s.db_path == s.data_dir / "buddy.db"


def test_env_override_and_dir_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERBUDDY_DATA_DIR", str(tmp_path / "bb"))
    monkeypatch.setenv("BROWSERBUDDY_PORT", "9999")
    s = get_settings()
    assert s.port == 9999
    assert s.data_dir == tmp_path / "bb"
    assert s.data_dir.is_dir()  # get_settings creates it
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'Settings'`.

- [ ] **Step 3: Implement `backend/src/browserbuddy/config.py`**

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration, overridable via BROWSERBUDDY_* env vars."""

    model_config = SettingsConfigDict(env_prefix="BROWSERBUDDY_")

    data_dir: Path = Path.home() / ".browserbuddy"
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.data_dir / "buddy.db"


def get_settings() -> Settings:
    """Load settings and ensure the data directory exists."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/config.py backend/tests/test_config.py
git commit -m "Add settings with env overrides and data dir creation"
```

---

### Task 3: Database connection, migrations, schema v1

**Files:**
- Create: `backend/src/browserbuddy/db.py`
- Create: `backend/tests/test_db.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_db.py`:

```python
from browserbuddy import db


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {r["name"] for r in rows}


def test_connect_applies_schema(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    tables = _tables(conn)
    assert {"history_entries", "open_tabs", "ingest_clients", "schema_version"} <= tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    db.connect(path).close()
    conn = db.connect(path)  # re-running migrations must not fail
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    conn.close()


def test_fts_stays_in_sync(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://example.com/quantum', 'Quantum Computing Intro', 1000, 'import')"
    )
    conn.commit()
    hit = conn.execute(
        "SELECT rowid FROM history_fts WHERE history_fts MATCH 'quantum'"
    ).fetchone()
    assert hit is not None
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ImportError` (module `browserbuddy.db` does not exist).

- [ ] **Step 3: Implement `backend/src/browserbuddy/db.py`**

```python
import sqlite3
from pathlib import Path

# Ordered migration scripts. Append new scripts; never edit shipped ones.
MIGRATIONS: list[str] = [
    # v1: Tier-1 schema — history (+FTS), open tabs, ingest client cursors.
    """
    CREATE TABLE history_entries (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        visit_time INTEGER NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('import', 'sync')),
        UNIQUE (url, visit_time)
    );

    CREATE INDEX idx_history_visit_time ON history_entries (visit_time);

    CREATE VIRTUAL TABLE history_fts USING fts5(
        url, title,
        content='history_entries', content_rowid='id'
    );

    CREATE TRIGGER history_ai AFTER INSERT ON history_entries BEGIN
        INSERT INTO history_fts(rowid, url, title)
        VALUES (new.id, new.url, new.title);
    END;

    CREATE TRIGGER history_ad AFTER DELETE ON history_entries BEGIN
        INSERT INTO history_fts(history_fts, rowid, url, title)
        VALUES ('delete', old.id, old.url, old.title);
    END;

    CREATE TRIGGER history_au AFTER UPDATE ON history_entries BEGIN
        INSERT INTO history_fts(history_fts, rowid, url, title)
        VALUES ('delete', old.id, old.url, old.title);
        INSERT INTO history_fts(rowid, url, title)
        VALUES (new.id, new.url, new.title);
    END;

    CREATE TABLE open_tabs (
        tab_id INTEGER NOT NULL,
        window_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        group_id INTEGER,
        container TEXT,
        opened_at INTEGER,
        last_activated_at INTEGER,
        active_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (window_id, tab_id)
    );

    CREATE TABLE ingest_clients (
        client_id TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the BrowserBuddy database and apply migrations."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row["version"] if row else 0
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(script)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_db.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/db.py backend/tests/test_db.py
git commit -m "Add SQLite schema v1 with migration runner and history FTS"
```

---

### Task 4: Ingest protocol v1 models

**Files:**
- Create: `backend/src/browserbuddy/models.py`
- Create: `backend/tests/test_models.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_models.py`:

```python
import pytest
from pydantic import ValidationError

from browserbuddy.models import EventEnvelope, HistoryVisitsEvent, TabStateEvent


def test_tab_state_envelope_roundtrip():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 7,
            "event": {
                "type": "tab_state",
                "tabs": [
                    {
                        "tab_id": 3,
                        "window_id": 1,
                        "url": "https://example.com",
                        "title": "Example",
                        "container": "Research",
                        "active_ms": 12000,
                    }
                ],
            },
        }
    )
    assert isinstance(env.event, TabStateEvent)
    assert env.event.tabs[0].container == "Research"
    assert env.event.tabs[0].group_id is None  # optional enhancement fields default


def test_history_visits_envelope():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 8,
            "event": {
                "type": "history_visits",
                "visits": [{"url": "https://example.com", "visit_time": 1720000000000}],
            },
        }
    )
    assert isinstance(env.event, HistoryVisitsEvent)
    assert env.event.visits[0].title == ""


def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({"v": 1, "seq": 1, "event": {"type": "bogus"}})


def test_seq_must_be_positive():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {"v": 1, "seq": 0, "event": {"type": "tab_state", "tabs": []}}
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `backend/src/browserbuddy/models.py`**

```python
from typing import Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


class HistoryVisit(BaseModel):
    url: str
    title: str = ""
    visit_time: int  # ms since epoch, UTC


class TabRecord(BaseModel):
    tab_id: int
    window_id: int
    url: str
    title: str = ""
    # Optional enrichment; Firefox-only fields (container) stay null on other browsers.
    group_id: int | None = None
    container: str | None = None
    opened_at: int | None = None
    last_activated_at: int | None = None
    active_ms: int = 0


class TabStateEvent(BaseModel):
    type: Literal["tab_state"]
    tabs: list[TabRecord]


class HistoryVisitsEvent(BaseModel):
    type: Literal["history_visits"]
    visits: list[HistoryVisit]


class AuthMessage(BaseModel):
    type: Literal["auth"]
    secret: str
    client_id: str


class EventEnvelope(BaseModel):
    """Versioned, sequence-numbered wrapper for every ingest event."""

    v: int = Field(ge=1)
    seq: int = Field(ge=1)
    event: TabStateEvent | HistoryVisitsEvent = Field(discriminator="type")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/models.py backend/tests/test_models.py
git commit -m "Add ingest protocol v1 models"
```

---

### Task 5: Auth — shared secret

**Files:**
- Create: `backend/src/browserbuddy/auth.py`
- Create: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_auth.py`:

```python
from browserbuddy.auth import get_or_create_secret, verify_secret


def test_secret_created_once_and_persisted(tmp_path):
    first = get_or_create_secret(tmp_path)
    second = get_or_create_secret(tmp_path)
    assert first == second
    assert len(first) >= 32
    secret_file = tmp_path / "secret"
    assert secret_file.exists()
    assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_verify_secret():
    assert verify_secret("abc", "abc")
    assert not verify_secret("abc", "abd")
    assert not verify_secret("abc", "")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `backend/src/browserbuddy/auth.py`**

```python
import hmac
import secrets
from pathlib import Path


def get_or_create_secret(data_dir: Path) -> str:
    """Return the shared secret, generating and persisting it on first run."""
    secret_file = data_dir / "secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_urlsafe(32)
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    return secret


def verify_secret(expected: str, provided: str) -> bool:
    return hmac.compare_digest(expected.encode(), provided.encode())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/auth.py backend/tests/test_auth.py
git commit -m "Add shared-secret creation and constant-time verification"
```

---

### Task 6: Storage — history insert + FTS search

**Files:**
- Create: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_storage_history.py`

- [ ] **Step 1: Write the shared fixture**

`backend/tests/conftest.py`:

```python
import pytest

from browserbuddy import db
from browserbuddy.storage import Storage


@pytest.fixture
def storage(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Storage(conn)
    conn.close()
```

- [ ] **Step 2: Write failing tests**

`backend/tests/test_storage_history.py`:

```python
from browserbuddy.models import HistoryVisit


def _visit(url, title="", t=1720000000000):
    return HistoryVisit(url=url, title=title, visit_time=t)


def test_insert_and_search(storage):
    n = storage.insert_history_visits(
        [
            _visit("https://arxiv.org/abs/1", "Quantum error correction survey"),
            _visit("https://example.com/cooking", "Pasta recipes"),
        ],
        source="import",
    )
    assert n == 2
    results = storage.search_history("quantum")
    assert len(results) == 1
    assert results[0]["url"] == "https://arxiv.org/abs/1"


def test_duplicate_visits_ignored(storage):
    visits = [_visit("https://example.com", "Example")]
    assert storage.insert_history_visits(visits, source="import") == 1
    assert storage.insert_history_visits(visits, source="sync") == 0


def test_search_survives_fts_special_chars(storage):
    storage.insert_history_visits([_visit("https://example.com", 'C++ "tricks"')], "import")
    # Must not raise an FTS5 syntax error.
    results = storage.search_history('c++ "tricks" OR')
    assert isinstance(results, list)


def test_search_empty_query_returns_empty(storage):
    assert storage.search_history("   ") == []
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_storage_history.py -v`
Expected: FAIL — `ImportError` (no `browserbuddy.storage`).

- [ ] **Step 4: Implement `backend/src/browserbuddy/storage.py`**

```python
import sqlite3

from browserbuddy.models import HistoryVisit


class Storage:
    """All database operations. One instance per connection; connection owns the lock."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- history ---------------------------------------------------------

    def insert_history_visits(self, visits: list[HistoryVisit], source: str) -> int:
        """Insert visits, silently skipping (url, visit_time) duplicates.

        Returns the number of rows actually inserted.
        """
        with self._conn:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO history_entries (url, title, visit_time, source)"
                " VALUES (?, ?, ?, ?)",
                [(v.url, v.title, v.visit_time, source) for v in visits],
            )
        return cur.rowcount

    def search_history(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Full-text search over history. User tokens are quoted, so FTS5
        operators in user input are treated as literals, not syntax."""
        tokens = query.split()
        if not tokens:
            return []
        fts_query = " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        return self._conn.execute(
            """
            SELECT h.url, h.title, h.visit_time
            FROM history_fts f
            JOIN history_entries h ON h.id = f.rowid
            WHERE history_fts MATCH ?
            ORDER BY bm25(history_fts), h.visit_time DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_storage_history.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/src/browserbuddy/storage.py backend/tests/conftest.py \
    backend/tests/test_storage_history.py
git commit -m "Add history storage with deduped inserts and FTS search"
```

---

### Task 7: Storage — open tabs + ingest client cursors

**Files:**
- Modify: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/test_storage_tabs.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_storage_tabs.py`:

```python
from browserbuddy.models import TabRecord


def _tab(tab_id, url, **kw):
    return TabRecord(tab_id=tab_id, window_id=1, url=url, **kw)


def test_replace_and_list_open_tabs(storage):
    storage.replace_open_tabs([_tab(1, "https://a.com", container="Research")])
    storage.replace_open_tabs(
        [_tab(1, "https://a.com", container="Research"), _tab(2, "https://b.com")]
    )
    tabs = storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://a.com", "https://b.com"]
    assert tabs[0]["container"] == "Research"
    assert tabs[1]["container"] is None


def test_replace_with_empty_clears(storage):
    storage.replace_open_tabs([_tab(1, "https://a.com")])
    storage.replace_open_tabs([])
    assert storage.list_open_tabs() == []


def test_client_seq_roundtrip(storage):
    assert storage.get_last_seq("ext-1") == 0
    storage.set_last_seq("ext-1", 41)
    storage.set_last_seq("ext-1", 42)
    assert storage.get_last_seq("ext-1") == 42
    assert storage.get_last_seq("other") == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storage_tabs.py -v`
Expected: FAIL — `AttributeError: 'Storage' object has no attribute 'replace_open_tabs'`.

- [ ] **Step 3: Extend `backend/src/browserbuddy/storage.py`**

Add `TabRecord` to the existing import and append these methods to `Storage`:

```python
    # -- open tabs -------------------------------------------------------

    def replace_open_tabs(self, tabs: list[TabRecord]) -> None:
        """Replace the entire current-tabs inventory with a fresh snapshot."""
        with self._conn:
            self._conn.execute("DELETE FROM open_tabs")
            self._conn.executemany(
                "INSERT INTO open_tabs (tab_id, window_id, url, title, group_id,"
                " container, opened_at, last_activated_at, active_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        t.tab_id,
                        t.window_id,
                        t.url,
                        t.title,
                        t.group_id,
                        t.container,
                        t.opened_at,
                        t.last_activated_at,
                        t.active_ms,
                    )
                    for t in tabs
                ],
            )

    def list_open_tabs(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM open_tabs ORDER BY window_id, tab_id"
        ).fetchall()

    # -- ingest client cursors --------------------------------------------

    def get_last_seq(self, client_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_seq FROM ingest_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return row["last_seq"] if row else 0

    def set_last_seq(self, client_id: str, seq: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO ingest_clients (client_id, last_seq) VALUES (?, ?)"
                " ON CONFLICT (client_id) DO UPDATE SET last_seq = excluded.last_seq",
                (client_id, seq),
            )
```

The import line at the top of `storage.py` becomes:

```python
from browserbuddy.models import HistoryVisit, TabRecord
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storage_tabs.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/storage.py backend/tests/test_storage_tabs.py
git commit -m "Add open-tab snapshots and ingest client sequence cursors"
```

---

### Task 8: FastAPI app — factory, /health, WebSocket ingest

**Files:**
- Create: `backend/src/browserbuddy/app.py`
- Create: `backend/tests/test_app.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Add app fixtures to `backend/tests/conftest.py`**

Append:

```python
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERBUDDY_DATA_DIR", str(tmp_path))
    from browserbuddy.app import create_app
    from browserbuddy.config import get_settings

    return create_app(get_settings())


@pytest.fixture
def client(app):
    return TestClient(app)
```

- [ ] **Step 2: Write failing tests**

`backend/tests/test_app.py`:

```python
from starlette.websockets import WebSocketDisconnect
import pytest


def _auth(ws, app, client_id="ext-test"):
    ws.send_json({"type": "auth", "secret": app.state.secret, "client_id": client_id})
    return ws.receive_json()


def _tab_state_envelope(seq, urls):
    return {
        "v": 1,
        "seq": seq,
        "event": {
            "type": "tab_state",
            "tabs": [{"tab_id": i, "window_id": 1, "url": u} for i, u in enumerate(urls)],
        },
    }


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "protocol": 1}


def test_ws_rejects_bad_secret(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "secret": "wrong", "client_id": "x"})
        ws.receive_json()


def test_ws_ingests_tab_state(client, app):
    with client.websocket_connect("/ws") as ws:
        assert _auth(ws, app) == {"type": "auth_ok", "last_seq": 0}
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    tabs = app.state.storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://a.com"]


def test_ws_replayed_seq_is_acked_but_not_applied(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        ws.receive_json()
        # Same seq, different content: must be acked (so the client advances)
        # but NOT applied (idempotency).
        ws.send_json(_tab_state_envelope(1, ["https://EVIL.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    assert [t["url"] for t in app.state.storage.list_open_tabs()] == ["https://a.com"]


def test_ws_resume_reports_last_seq(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        ws.receive_json()
    with client.websocket_connect("/ws") as ws:
        assert _auth(ws, app) == {"type": "auth_ok", "last_seq": 1}


def test_ws_malformed_event_gets_error_not_disconnect(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json({"v": 1, "seq": 1, "event": {"type": "bogus"}})
        assert ws.receive_json()["type"] == "error"
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_history_visits_ingested(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            {
                "v": 1,
                "seq": 1,
                "event": {
                    "type": "history_visits",
                    "visits": [
                        {
                            "url": "https://arxiv.org/abs/2",
                            "title": "Retrieval survey",
                            "visit_time": 1720000000000,
                        }
                    ],
                },
            }
        )
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    results = app.state.storage.search_history("retrieval")
    assert len(results) == 1
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: browserbuddy.app`.

- [ ] **Step 4: Implement `backend/src/browserbuddy/app.py`**

```python
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret, verify_secret
from browserbuddy.config import Settings, get_settings
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    TabStateEvent,
)
from browserbuddy.storage import Storage

logger = logging.getLogger(__name__)

# Application-defined close code for failed auth (4000-4999 range).
WS_CLOSE_UNAUTHORIZED = 4401


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="BrowserBuddy")
    app.state.storage = Storage(db.connect(settings.db_path))
    app.state.secret = get_or_create_secret(settings.data_dir)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "protocol": PROTOCOL_VERSION}

    @app.websocket("/ws")
    async def ws_ingest(ws: WebSocket) -> None:
        storage: Storage = app.state.storage
        await ws.accept()
        try:
            auth = AuthMessage.model_validate(await ws.receive_json())
        except (ValidationError, ValueError):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        if not verify_secret(app.state.secret, auth.secret):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return

        last_seq = storage.get_last_seq(auth.client_id)
        await ws.send_json({"type": "auth_ok", "last_seq": last_seq})

        try:
            while True:
                raw = await ws.receive_json()
                try:
                    envelope = EventEnvelope.model_validate(raw)
                except ValidationError as exc:
                    logger.warning("Rejected malformed event: %s", exc)
                    await ws.send_json({"type": "error", "detail": exc.errors()[0]["msg"]})
                    continue
                if envelope.seq > last_seq:
                    _apply_event(storage, envelope)
                    storage.set_last_seq(auth.client_id, envelope.seq)
                    last_seq = envelope.seq
                await ws.send_json({"type": "ack", "seq": envelope.seq})
        except WebSocketDisconnect:
            pass

    return app


def _apply_event(storage: Storage, envelope: EventEnvelope) -> None:
    event = envelope.event
    if isinstance(event, TabStateEvent):
        storage.replace_open_tabs(event.tabs)
    elif isinstance(event, HistoryVisitsEvent):
        storage.insert_history_visits(event.visits, source="sync")
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_app.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py backend/tests/conftest.py
git commit -m "Add FastAPI app with authenticated, idempotent WebSocket ingest"
```

---

### Task 9: Firefox places.sqlite importer

**Files:**
- Create: `backend/src/browserbuddy/importer/__init__.py` (empty)
- Create: `backend/src/browserbuddy/importer/firefox.py`
- Create: `backend/tests/test_importer_firefox.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_importer_firefox.py`:

```python
import sqlite3

import pytest

from browserbuddy.importer.firefox import find_default_profile, import_places


def make_places(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_historyvisits (
            id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER
        );
        """
    )
    for i, (url, title, visit_date_us) in enumerate(rows, start=1):
        conn.execute("INSERT INTO moz_places VALUES (?, ?, ?)", (i, url, title))
        conn.execute(
            "INSERT INTO moz_historyvisits VALUES (?, ?, ?)", (i, i, visit_date_us)
        )
    conn.commit()
    conn.close()


def test_import_places(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    # visit_date is in MICROseconds in Firefox; importer must convert to ms.
    make_places(
        places,
        [
            ("https://arxiv.org/abs/3", "Attention is all you need", 1720000000000000),
            ("https://example.com", None, 1720000001000000),  # NULL title
        ],
    )
    count = import_places(places, storage)
    assert count == 2
    results = storage.search_history("attention")
    assert results[0]["visit_time"] == 1720000000000  # µs -> ms


def test_import_is_idempotent(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    make_places(places, [("https://a.com", "A", 1720000000000000)])
    assert import_places(places, storage) == 1
    assert import_places(places, storage) == 0


def test_find_default_profile(tmp_path):
    (tmp_path / "profiles.ini").write_text(
        "[Install0123ABCD]\n"
        "Default=Profiles/xyz.default-release\n"
        "\n"
        "[Profile0]\n"
        "Name=default-release\n"
        "IsRelative=1\n"
        "Path=Profiles/xyz.default-release\n"
    )
    (tmp_path / "Profiles" / "xyz.default-release").mkdir(parents=True)
    assert find_default_profile(tmp_path) == tmp_path / "Profiles" / "xyz.default-release"


def test_find_default_profile_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_default_profile(tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_importer_firefox.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `backend/src/browserbuddy/importer/firefox.py`**

(Also create empty `backend/src/browserbuddy/importer/__init__.py`.)

```python
"""Firefox history backfill: read a copy of places.sqlite into BrowserBuddy.

This is the Firefox implementation of the pluggable per-browser importer
seam described in the design spec; a Chrome importer would expose the same
two functions against Chrome's History database.
"""

import configparser
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from browserbuddy.models import HistoryVisit
from browserbuddy.storage import Storage

BATCH_SIZE = 1000


def firefox_root() -> Path:
    """Platform-default Firefox data directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


def find_default_profile(root: Path) -> Path:
    """Locate the default profile directory via profiles.ini."""
    ini_path = root / "profiles.ini"
    if not ini_path.exists():
        raise FileNotFoundError(
            f"No profiles.ini in {root} — is Firefox installed? Pass --profile explicitly."
        )
    ini = configparser.ConfigParser()
    ini.read(ini_path)

    candidates: list[str] = []
    for section in ini.sections():
        if section.startswith("Install") and ini[section].get("Default"):
            candidates.append(ini[section]["Default"])
    for section in ini.sections():
        if section.startswith("Profile") and ini[section].get("Default") == "1":
            candidates.append(ini[section]["Path"])

    for rel in candidates:
        profile = root / rel
        if profile.is_dir():
            return profile
    raise FileNotFoundError(
        f"profiles.ini in {root} lists no existing default profile. Pass --profile."
    )


def import_places(places_path: Path, storage: Storage) -> int:
    """Import all visits from a places.sqlite file. Returns rows inserted.

    Works on a temp copy so a running Firefox (which locks the DB) is never
    touched. Firefox stores visit_date in microseconds; we store milliseconds.
    """
    if not places_path.exists():
        raise FileNotFoundError(f"No such file: {places_path}")

    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "places.sqlite"
        shutil.copy2(places_path, copy)
        wal = places_path.with_name(places_path.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, copy.with_name(copy.name + "-wal"))

        conn = sqlite3.connect(copy)
        try:
            cursor = conn.execute(
                """
                SELECT p.url, IFNULL(p.title, ''), v.visit_date / 1000
                FROM moz_historyvisits v
                JOIN moz_places p ON p.id = v.place_id
                ORDER BY v.visit_date
                """
            )
            while batch := cursor.fetchmany(BATCH_SIZE):
                visits = [
                    HistoryVisit(url=url, title=title, visit_time=visit_ms)
                    for url, title, visit_ms in batch
                ]
                total += storage.insert_history_visits(visits, source="import")
        finally:
            conn.close()
    return total
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_importer_firefox.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/importer backend/tests/test_importer_firefox.py
git commit -m "Add Firefox places.sqlite history importer"
```

---

### Task 10: CLI

**Files:**
- Create: `backend/src/browserbuddy/cli.py`
- Create: `backend/tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

`backend/tests/test_cli.py`:

```python
import sqlite3

from typer.testing import CliRunner

from browserbuddy.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {"BROWSERBUDDY_DATA_DIR": str(tmp_path)}


def _seed_history(tmp_path):
    from browserbuddy import db
    from browserbuddy.models import HistoryVisit
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    Storage(conn).insert_history_visits(
        [HistoryVisit(url="https://arxiv.org/abs/9", title="Vector DB survey",
                      visit_time=1720000000000)],
        source="import",
    )
    conn.close()


def test_secret_prints_secret(tmp_path):
    result = runner.invoke(app, ["secret"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert (tmp_path / "secret").read_text().strip() in result.output


def test_search_finds_seeded_history(tmp_path):
    _seed_history(tmp_path)
    result = runner.invoke(app, ["search", "vector"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "https://arxiv.org/abs/9" in result.output


def test_search_no_results(tmp_path):
    _seed_history(tmp_path)
    result = runner.invoke(app, ["search", "zebra"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No results" in result.output


def test_tabs_empty(tmp_path):
    result = runner.invoke(app, ["tabs"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No open tabs" in result.output


def test_import_history_with_explicit_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    conn = sqlite3.connect(profile / "places.sqlite")
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_historyvisits (
            id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER
        );
        INSERT INTO moz_places VALUES (1, 'https://a.com', 'A');
        INSERT INTO moz_historyvisits VALUES (1, 1, 1720000000000000);
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["import-history", "--profile", str(profile)], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "Imported 1" in result.output


def test_import_history_missing_profile_fails_clearly(tmp_path):
    result = runner.invoke(
        app, ["import-history", "--profile", str(tmp_path / "nope")], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "places.sqlite" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: browserbuddy.cli`.

- [ ] **Step 3: Implement `backend/src/browserbuddy/cli.py`**

```python
from datetime import UTC, datetime
from pathlib import Path

import typer

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret
from browserbuddy.config import get_settings
from browserbuddy.importer import firefox
from browserbuddy.storage import Storage

app = typer.Typer(help="BrowserBuddy — your browser's research historian.")


def _storage() -> Storage:
    settings = get_settings()
    return Storage(db.connect(settings.db_path))


def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


@app.command()
def serve() -> None:
    """Run the ingest server (foreground)."""
    import uvicorn

    from browserbuddy.app import create_app

    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


@app.command()
def secret() -> None:
    """Print the shared secret to paste into the extension's options page."""
    typer.echo(get_or_create_secret(get_settings().data_dir))


@app.command("import-history")
def import_history(
    profile: Path | None = typer.Option(
        None, help="Firefox profile directory (default: auto-detect)."
    ),
) -> None:
    """Backfill browser history from Firefox's places.sqlite."""
    if profile is None:
        try:
            profile = firefox.find_default_profile(firefox.firefox_root())
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
    places = profile / "places.sqlite"
    try:
        count = firefox.import_places(places, _storage())
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Imported {count} new history entries from {places}")


@app.command()
def search(
    query: str,
    limit: int = typer.Option(20, help="Maximum results."),
) -> None:
    """Full-text search over captured browser history."""
    results = _storage().search_history(query, limit=limit)
    if not results:
        typer.echo("No results.")
        return
    for row in results:
        typer.echo(f"{_fmt_time(row['visit_time'])}  {row['title'] or '(untitled)'}")
        typer.echo(f"                  {row['url']}")


@app.command()
def tabs() -> None:
    """Show the current open-tab inventory (as last synced by the extension)."""
    rows = _storage().list_open_tabs()
    if not rows:
        typer.echo("No open tabs recorded yet. Is the extension connected?")
        return
    for row in rows:
        container = f" [{row['container']}]" if row["container"] else ""
        typer.echo(f"w{row['window_id']} t{row['tab_id']}{container}  "
                   f"{row['title'] or '(untitled)'}")
        typer.echo(f"        {row['url']}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 6 passed.

- [ ] **Step 5: Sanity-check the installed entry point**

Run: `uv run buddy --help`
Expected: help text listing `serve`, `secret`, `import-history`, `search`, `tabs`.

- [ ] **Step 6: Commit**

```bash
git add backend/src/browserbuddy/cli.py backend/tests/test_cli.py
git commit -m "Add buddy CLI: serve, secret, import-history, search, tabs"
```

---

### Task 11: Zero-warnings pass + README

**Files:**
- Create: `README.md`
- Modify: anything ruff flags

- [ ] **Step 1: Run the full gate**

```bash
cd backend
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

Expected: no ruff findings (fix every warning — zero-warnings policy; use
`ruff format .` to fix formatting), all tests pass.

- [ ] **Step 2: Write `README.md` (repo root)**

```markdown
# BrowserBuddy

A Firefox extension + local backend that acts as a personal research
historian: explicit research sessions, browsing memory, and an agent that
can answer "what did I read about X last week?"

Design spec: `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`

## Backend quick start

```bash
cd backend
uv sync
uv run buddy import-history   # backfill Firefox history
uv run buddy search "quantum" # search it
uv run buddy serve            # start the ingest server (127.0.0.1:8765)
uv run buddy secret           # secret to paste into the extension
```

Data lives in `~/.browserbuddy/` (SQLite + secret). Override with
`BROWSERBUDDY_DATA_DIR`.
```

- [ ] **Step 3: Manual smoke test against real data**

```bash
cd backend
uv run buddy import-history
uv run buddy search "github"
```

Expected: real history entries print. (If Firefox isn't installed on this
machine, skip — the fixture-based tests cover the logic.)

- [ ] **Step 4: Commit**

```bash
git add README.md backend
git commit -m "Add README and pass zero-warnings gate"
```

---

## Follow-ups for the extension (Tier-1) milestone

Recorded from the final whole-branch review (2026-07-15); none block this
milestone:

1. **Stale concurrent WS connection can apply an out-of-order `tab_state`.**
   The seq cursor is monotonic in the DB, but each connection caches
   `last_seq` locally — a zombie MV3-worker socket can overwrite a newer tab
   snapshot with an older one (self-healing on next snapshot). Fix with a
   per-event DB cursor check or a single-connection-per-client_id policy.
2. **Corrupt `places.sqlite` yields a raw traceback.** CLI catches only
   FileNotFoundError; catch `sqlite3.DatabaseError` in `import-history` with
   an actionable message. A negative `visit_date` row would also abort the
   import via the `ge=0` validator — filter in SQL like the NULL case.
3. Minor: app lifespan doesn't close the DB connection; `/ws` accept should
   carry a one-line comment recording the secret-not-origin decision; sync
   DB calls on the event loop will matter at Tier-2 ingest volumes; `--limit`
   help doesn't mention the 200 cap.

## Self-review notes

- **Spec coverage (this milestone):** storage schema v1 ✓, ingest protocol with versioning/seq/ack/idempotency ✓, secret-not-origin auth ✓ (no origin checks anywhere), 127.0.0.1 binding ✓ (Settings default + uvicorn host), pluggable importer seam ✓ (module-level interface documented in `firefox.py` docstring), container/tab-group fields in schema ✓. Deferred to later plans by design: sessions/page_visits tables, embeddings, agent, MCP, digests, dashboard, extension.
- **Types are consistent across tasks:** `Storage` methods introduced in Tasks 6–7 match their uses in Tasks 8–10; `HistoryVisit`/`TabRecord` fields match schema columns.
- **Known simplification:** `open_tabs` stores only the latest snapshot (replace-all), per spec's "current inventory" tool need; no snapshot history in v1.
```
