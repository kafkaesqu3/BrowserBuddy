# BrowserBuddy M3b Slice A (Link graph & nav provenance) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the navigation layer of a research session — every page's outbound links, which links were clicked, and the navigation edges (how B was reached from A) with search-engine provenance — stored in two new tables and readable via `buddy links`.

**Architecture:** Additive schema v3 (`nav_edges`, `outbound_links`) + two new protocol events (`nav_edge`, `link_click`) and a `links` field on `page_capture`. Three capture streams: the content script catalogues `<a href>` links and reports anchor clicks; the background `webNavigation` listener emits nav edges with per-tab from-URL tracking and search-provenance derivation. Backend dispatch and a `buddy links` CLI read surface round it out. Everything is gated on an active session (unknown session → `NoActiveSessionError` → error frame, existing pattern).

**Tech Stack:** existing stacks — Python (FastAPI, Pydantic, SQLite, Typer, pytest) and TypeScript (Vitest, happy-dom, Vite, `webNavigation`/`webRequest`-free — nav data from `webNavigation` only). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-30-m3b-slice-a-link-graph-design.md`

Conventions unchanged: ms epoch UTC timestamps; imports never `../` (use `@src/*` in extension tests); backend `NoActiveSessionError` is the "no active session" signal caught by the WS loop; additive protocol under v1 (older backends error-frame unknown types, clients skip). Backend gates: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` (from `backend/`). Extension gates: `npm run check` (from `extension/`). Baselines before this plan: extension 58 tests; backend suite green.

> **Env note for agentic workers:** spawned subagents in this environment are often denied Bash for `npm`/`uv`/`git`. If a command is denied, make all file edits, then STOP and report BLOCKED with the exact command — the controller runs gates and commits. `cd` does not persist between commands; use `npm --prefix extension run ...` and `uv run --project backend ...` or a single-line `( cd backend && ... )`.

---

## File structure

```
backend/src/browserbuddy/
  db.py          # + MIGRATIONS[2] (schema v3: nav_edges, outbound_links)
  models.py      # + OutboundLink; PageCaptureEvent.links; LinkClickEvent; NavEdgeEvent; union
  storage.py     # + add_outbound_links, add_nav_edge, mark_link_clicked, list_nav_edges,
                 #   list_unfollowed_links; add_page_visit now returns the page_visit id
  app.py         # + dispatch nav_edge, link_click, outbound-links on page_capture
  cli.py         # + `buddy links` + _resolve_session helper
backend/tests/
  test_db.py, test_storage_links.py (NEW), test_storage_sessions.py (update),
  test_models.py, test_app.py, test_cli.py

extension/src/
  types.ts       # + OutboundLink; PageCapture.links; link_click/nav_edge EventPayload arms
  extract.ts     # + extractLinks(doc)
  capture.ts     # + MAX_LINKS, links on pageCaptureEvent, linkClickEvent()
  navigation.ts  # NEW: searchProvenance(url), navEdgeEvent(input)
  content.ts     # + collect links, click/auxclick listener
  background.ts  # + webNavigation listeners, per-tab url map, bb:click handling
extension/manifest.firefox.json, manifest.chrome.json  # + "webNavigation"
extension/tests/
  extract.test.ts (update), capture.test.ts (update), navigation.test.ts (NEW),
  manifest.test.ts (update)
```

---

## Task 1: Backend — schema v3 (nav_edges, outbound_links)

**Files:**
- Modify: `backend/src/browserbuddy/db.py`
- Modify: `backend/tests/test_db.py`

- [ ] **Step 1: Write failing migration test** — append to `backend/tests/test_db.py`:

```python
def test_v3_migration_adds_link_graph_tables_and_keeps_data(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://keep.me', 'Keep', 1000, 'import')"
    )
    conn.execute(
        "INSERT INTO sessions (client_id, name, started_at) VALUES ('c', 's', 1)"
    )
    conn.commit()
    conn.close()
    conn = db.connect(path)  # re-running migrations applies any new versions
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    tables = _tables(conn)
    assert {"nav_edges", "outbound_links"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM history_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()
```

(`_tables` is the existing helper in this file, used by the v2 test.)

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_db.py -v -k v3`
Expected: FAIL — MAX(version) is 2; no `nav_edges`/`outbound_links`.

- [ ] **Step 3: Append migration v3 to `MIGRATIONS` in `backend/src/browserbuddy/db.py`** (add a new string element after the v2 script; do not edit v1/v2):

```python
    # v3: Tier-2 link graph — navigation edges and per-page outbound links.
    """
    CREATE TABLE nav_edges (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions (id),
        from_url TEXT,
        to_url TEXT NOT NULL,
        transition TEXT NOT NULL DEFAULT '',
        qualifiers TEXT NOT NULL DEFAULT '',
        query TEXT,
        occurred_at INTEGER NOT NULL,
        UNIQUE (session_id, from_url, to_url, occurred_at)
    );

    CREATE INDEX idx_nav_edges_session ON nav_edges (session_id);

    CREATE TABLE outbound_links (
        id INTEGER PRIMARY KEY,
        page_visit_id INTEGER NOT NULL REFERENCES page_visits (id),
        href TEXT NOT NULL,
        anchor_text TEXT NOT NULL DEFAULT '',
        rel TEXT NOT NULL DEFAULT '',
        clicked INTEGER NOT NULL DEFAULT 0,
        UNIQUE (page_visit_id, href)
    );

    CREATE INDEX idx_outbound_links_page ON outbound_links (page_visit_id);
    """,
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_db.py -v -k v3`: PASS. Full file green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/db.py backend/tests/test_db.py
git commit -m "Add schema v3 with nav_edges and outbound_links"
```

---

## Task 2: Backend — storage methods (+ add_page_visit returns id)

**Files:**
- Modify: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/test_storage_links.py`
- Modify: `backend/tests/test_storage_sessions.py` (update the `add_page_visit` return-value assertions)

- [ ] **Step 1: Write failing tests** — create `backend/tests/test_storage_links.py`:

```python
from browserbuddy.models import OutboundLink


def _page(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    pid = storage.add_page_visit(
        sid, url="https://a.com", title="A", visited_at=1100,
        text="", html_key=None, container=None,
    )
    return sid, pid


def test_add_page_visit_returns_id_stable_across_duplicate(storage):
    sid, pid = _page(storage)
    assert pid > 0
    # duplicate (session, url, visited_at) returns the SAME id, not a new row
    again = storage.add_page_visit(
        sid, url="https://a.com", title="x", visited_at=1100,
        text="y", html_key=None, container=None,
    )
    assert again == pid


def test_outbound_links_insert_dedup_and_unfollowed(storage):
    sid, pid = _page(storage)
    n = storage.add_outbound_links(
        pid,
        [
            OutboundLink(href="https://x.com", text="X", rel="nofollow"),
            OutboundLink(href="https://y.com", text="Y", rel=""),
        ],
    )
    assert n == 2
    # duplicate href for the same page is ignored
    assert storage.add_outbound_links(pid, [OutboundLink(href="https://x.com", text="X2")]) == 0
    unfollowed = storage.list_unfollowed_links(sid)
    hrefs = {r["href"] for r in unfollowed}
    assert hrefs == {"https://x.com", "https://y.com"}


def test_mark_link_clicked_marks_catalogued_and_inserts_dynamic(storage):
    sid, pid = _page(storage)
    storage.add_outbound_links(pid, [OutboundLink(href="https://x.com", text="X")])
    # click on a catalogued link removes it from the un-followed set
    assert storage.mark_link_clicked(sid, page_url="https://a.com", href="https://x.com", text="X") is True
    assert all(r["href"] != "https://x.com" for r in storage.list_unfollowed_links(sid))
    # click on a link not in the catalogue inserts it, already-clicked
    assert storage.mark_link_clicked(sid, page_url="https://a.com", href="https://z.com", text="Z") is True
    assert all(r["href"] != "https://z.com" for r in storage.list_unfollowed_links(sid))


def test_mark_link_clicked_orphan_returns_false(storage):
    sid, _ = _page(storage)
    assert storage.mark_link_clicked(sid, page_url="https://never-seen.com", href="https://x.com", text="") is False


def test_add_nav_edge_dedup_and_list_order(storage):
    sid, _ = _page(storage)
    assert storage.add_nav_edge(
        sid, from_url="https://google.com/search?q=vec", to_url="https://a.com",
        transition="link", qualifiers="", query="vec", occurred_at=1200,
    ) == 1
    # exact duplicate (session, from, to, occurred_at) ignored
    assert storage.add_nav_edge(
        sid, from_url="https://google.com/search?q=vec", to_url="https://a.com",
        transition="link", qualifiers="", query="vec", occurred_at=1200,
    ) == 0
    storage.add_nav_edge(
        sid, from_url="https://a.com", to_url="https://b.com",
        transition="link", qualifiers="", query=None, occurred_at=1300,
    )
    edges = storage.list_nav_edges(sid)
    assert [e["to_url"] for e in edges] == ["https://a.com", "https://b.com"]  # occurred_at order
    assert edges[0]["query"] == "vec"
```

Also **update** `backend/tests/test_storage_sessions.py` `test_add_page_visit_and_search`: replace the `assert n == 1` block and the duplicate `== 0` assertion so the local variable is captured as an id. Change:

```python
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
    assert (
        storage.add_page_visit(
            sid,
            url="https://arxiv.org/abs/42",
            title="x",
            visited_at=1500,
            text="y",
            html_key=None,
            container=None,
        )
        == 0
    )
```

to:

```python
    pid = storage.add_page_visit(
        sid,
        url="https://arxiv.org/abs/42",
        title="Sparse retrieval methods",
        visited_at=1500,
        text="A survey of sparse retrieval and ranking methods.",
        html_key="abc123",
        container="Research",
    )
    assert pid > 0
    # duplicate (session, url, visited_at) returns the same id (no new row)
    assert (
        storage.add_page_visit(
            sid,
            url="https://arxiv.org/abs/42",
            title="x",
            visited_at=1500,
            text="y",
            html_key=None,
            container=None,
        )
        == pid
    )
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_storage_links.py tests/test_storage_sessions.py -v`
Expected: FAIL — `add_outbound_links`/`add_nav_edge`/`mark_link_clicked`/`list_*` missing; `add_page_visit` still returns rowcount.

- [ ] **Step 3: Edit `backend/src/browserbuddy/storage.py`**

Add `OutboundLink` to the models import at the top:

```python
from browserbuddy.models import HistoryVisit, OutboundLink, TabRecord
```

Replace the body of `add_page_visit` (keep the signature and the exists-check) so it returns the id:

```python
        exists = self._conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if exists is None:
            raise NoActiveSessionError(f"no such session: {session_id}")
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO page_visits"
                " (session_id, url, title, visited_at, text, html_key, container)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, url, title, visited_at, text, html_key, container),
            )
            if cur.rowcount:
                return cur.lastrowid
            row = self._conn.execute(
                "SELECT id FROM page_visits WHERE session_id = ? AND url = ? AND visited_at = ?",
                (session_id, url, visited_at),
            ).fetchone()
            return row["id"]
```

Update its docstring first line to: `"""Record a captured page, returning its page_visit id (the existing id on a duplicate so outbound links still attach)."""` (keep the NoActiveSessionError note).

Append these methods to the `Storage` class (after `search_pages`):

```python
    # -- link graph --------------------------------------------------------

    def add_outbound_links(self, page_visit_id: int, links: list[OutboundLink]) -> int:
        """Catalogue a page's outbound links. Duplicate (page, href) ignored."""
        with self._conn:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO outbound_links (page_visit_id, href, anchor_text, rel)"
                " VALUES (?, ?, ?, ?)",
                [(page_visit_id, link.href, link.text, link.rel) for link in links],
            )
        return cur.rowcount

    def add_nav_edge(
        self,
        session_id: int,
        *,
        from_url: str | None,
        to_url: str,
        transition: str,
        qualifiers: str,
        query: str | None,
        occurred_at: int,
    ) -> int:
        """Record a navigation edge. Exact-duplicate edges are ignored."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO nav_edges"
                " (session_id, from_url, to_url, transition, qualifiers, query, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, from_url, to_url, transition, qualifiers, query, occurred_at),
            )
        return cur.rowcount

    def mark_link_clicked(self, session_id: int, *, page_url: str, href: str, text: str) -> bool:
        """Mark an outbound link clicked on the latest visit of page_url in this
        session. Inserts the row if the link was not in the catalogue (e.g. a
        dynamically added link). Returns False when the page was never captured
        (orphan click — dropped by the caller)."""
        row = self._conn.execute(
            "SELECT id FROM page_visits WHERE session_id = ? AND url = ?"
            " ORDER BY visited_at DESC LIMIT 1",
            (session_id, page_url),
        ).fetchone()
        if row is None:
            return False
        with self._conn:
            self._conn.execute(
                "INSERT INTO outbound_links (page_visit_id, href, anchor_text, clicked)"
                " VALUES (?, ?, ?, 1)"
                " ON CONFLICT (page_visit_id, href) DO UPDATE SET clicked = 1",
                (row["id"], href, text),
            )
        return True

    def list_nav_edges(self, session_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT from_url, to_url, transition, qualifiers, query, occurred_at"
            " FROM nav_edges WHERE session_id = ? ORDER BY occurred_at, id",
            (session_id,),
        ).fetchall()

    def list_unfollowed_links(self, session_id: int, limit: int = 50) -> list[sqlite3.Row]:
        """Outbound links seen but not clicked, for the digest's 'notable
        un-followed links'."""
        return self._conn.execute(
            "SELECT o.href, o.anchor_text, p.url AS page_url"
            " FROM outbound_links o JOIN page_visits p ON p.id = o.page_visit_id"
            " WHERE p.session_id = ? AND o.clicked = 0"
            " ORDER BY p.visited_at, o.id LIMIT ?",
            (session_id, limit),
        ).fetchall()
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_storage_links.py tests/test_storage_sessions.py -q`: PASS. `uv run ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/storage.py backend/tests/test_storage_links.py \
    backend/tests/test_storage_sessions.py
git commit -m "Add link-graph storage methods and page-visit id return"
```

---

## Task 3: Backend — protocol models

**Files:**
- Modify: `backend/src/browserbuddy/models.py`
- Modify: `backend/tests/test_models.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_models.py`:

```python
def test_page_capture_accepts_links_and_defaults_empty():
    env = EventEnvelope.model_validate(
        {
            "v": 1, "seq": 1,
            "event": {
                "type": "page_capture", "url": "https://a.com", "visited_at": 5,
                "links": [{"href": "https://x.com", "text": "X", "rel": "nofollow"}],
            },
        }
    )
    assert env.event.links[0].href == "https://x.com"
    env2 = EventEnvelope.model_validate(
        {"v": 1, "seq": 2, "event": {"type": "page_capture", "url": "https://a.com", "visited_at": 5}}
    )
    assert env2.event.links == []


def test_nav_edge_and_link_click_validate():
    env = EventEnvelope.model_validate(
        {
            "v": 1, "seq": 1,
            "event": {
                "type": "nav_edge", "to_url": "https://a.com", "occurred_at": 7,
                "from_url": "https://google.com/search?q=x", "transition": "link",
                "qualifiers": "client_redirect", "query": "x",
            },
        }
    )
    assert env.event.type == "nav_edge"
    assert env.event.from_url == "https://google.com/search?q=x"
    env2 = EventEnvelope.model_validate(
        {"v": 1, "seq": 2, "event": {"type": "nav_edge", "to_url": "https://a.com", "occurred_at": 7}}
    )
    assert env2.event.from_url is None and env2.event.query is None
    env3 = EventEnvelope.model_validate(
        {
            "v": 1, "seq": 3,
            "event": {"type": "link_click", "page_url": "https://a.com", "href": "https://x.com", "clicked_at": 9},
        }
    )
    assert env3.event.type == "link_click" and env3.event.text == ""
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_models.py -v -k "links or nav_edge or link_click"`: FAIL (unknown types / no `links`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/models.py`**

Add `OutboundLink` (before `PageCaptureEvent`):

```python
class OutboundLink(ProtocolModel):
    href: str = Field(min_length=1)
    text: str = ""
    rel: str = ""
```

Add `links` to `PageCaptureEvent` (after `container`):

```python
    links: list[OutboundLink] = []
```

Add the two new events (after `PageCaptureEvent`):

```python
class LinkClickEvent(ProtocolModel):
    type: Literal["link_click"]
    page_url: str = Field(min_length=1)
    href: str = Field(min_length=1)
    text: str = ""
    clicked_at: int = Field(ge=0)


class NavEdgeEvent(ProtocolModel):
    type: Literal["nav_edge"]
    from_url: str | None = None
    to_url: str = Field(min_length=1)
    transition: str = ""
    qualifiers: str = ""
    query: str | None = None
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
    ) = Field(discriminator="type")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_models.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/models.py backend/tests/test_models.py
git commit -m "Add outbound-link, nav-edge, and link-click protocol models"
```

---

## Task 4: Backend — ingest dispatch

**Files:**
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_app.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_app.py` (the file already has `_envelope`, `_auth`, and `client`/`app` fixtures from M3a):

```python
def test_ws_page_capture_stores_outbound_links(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(2, {
                "type": "page_capture", "url": "https://a.com", "visited_at": 150,
                "text": "body", "links": [{"href": "https://x.com", "text": "X"}],
            })
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
    storage = app.state.storage
    sid = storage.list_sessions()[0]["id"]
    assert {r["href"] for r in storage.list_unfollowed_links(sid)} == {"https://x.com"}


def test_ws_nav_edge_and_link_click(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(2, {
                "type": "page_capture", "url": "https://a.com", "visited_at": 150,
                "text": "b", "links": [{"href": "https://x.com", "text": "X"}],
            })
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(
            _envelope(3, {
                "type": "nav_edge", "from_url": "https://a.com", "to_url": "https://x.com",
                "transition": "link", "occurred_at": 160,
            })
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
        ws.send_json(
            _envelope(4, {
                "type": "link_click", "page_url": "https://a.com", "href": "https://x.com",
                "text": "X", "clicked_at": 165,
            })
        )
        assert ws.receive_json() == {"type": "ack", "seq": 4}
    storage = app.state.storage
    sid = storage.list_sessions()[0]["id"]
    edges = storage.list_nav_edges(sid)
    assert edges[0]["to_url"] == "https://x.com"
    # the click marked the catalogued link followed
    assert all(r["href"] != "https://x.com" for r in storage.list_unfollowed_links(sid))


def test_ws_nav_edge_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "nav_edge", "to_url": "https://a.com", "occurred_at": 1}))
        assert ws.receive_json()["type"] == "error"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_app.py -v -k "outbound or nav_edge or link_click"`: FAIL (dispatch raises the `unhandled event type` AssertionError for the new types; links ignored).

- [ ] **Step 3: Edit `backend/src/browserbuddy/app.py`**

Add the new events to the models import:

```python
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    LinkClickEvent,
    NavEdgeEvent,
    PageCaptureEvent,
    SessionEndEvent,
    SessionStartEvent,
    TabStateEvent,
)
```

In `_apply_event`, replace the `PageCaptureEvent` branch and add the two new branches (before the `else`):

```python
    elif isinstance(event, PageCaptureEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_capture with no active session")
        html_key = blobs.put(event.html.encode()) if event.html else None
        page_visit_id = storage.add_page_visit(
            active["id"],
            url=event.url,
            title=event.title,
            visited_at=event.visited_at,
            text=event.text,
            html_key=html_key,
            container=event.container,
        )
        if event.links:
            storage.add_outbound_links(page_visit_id, event.links)
    elif isinstance(event, NavEdgeEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("nav_edge with no active session")
        storage.add_nav_edge(
            active["id"],
            from_url=event.from_url,
            to_url=event.to_url,
            transition=event.transition,
            qualifiers=event.qualifiers,
            query=event.query,
            occurred_at=event.occurred_at,
        )
    elif isinstance(event, LinkClickEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("link_click with no active session")
        storage.mark_link_clicked(
            active["id"], page_url=event.page_url, href=event.href, text=event.text
        )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_app.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py
git commit -m "Dispatch nav-edge, link-click, and outbound-link ingest"
```

---

## Task 5: Backend — `buddy links` CLI

**Files:**
- Modify: `backend/src/browserbuddy/cli.py`
- Modify: `backend/tests/test_cli.py`

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_cli.py` (uses the existing `runner`, `app`, and `_env(tmp_path)` helpers):

```python
def _seed_links(tmp_path):
    from browserbuddy import db
    from browserbuddy.models import OutboundLink
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    s = Storage(conn)
    sid = s.start_session("ext-1", "graph session", started_at=1720000000000)
    pid = s.add_page_visit(
        sid, url="https://a.com", title="A", visited_at=1720000001000,
        text="", html_key=None, container=None,
    )
    s.add_outbound_links(pid, [OutboundLink(href="https://unfollowed.com", text="Later")])
    s.add_nav_edge(
        sid, from_url="https://google.com/search?q=vectors", to_url="https://a.com",
        transition="link", qualifiers="", query="vectors", occurred_at=1720000000500,
    )
    conn.close()


def test_links_shows_edges_and_unfollowed(tmp_path):
    _seed_links(tmp_path)
    result = runner.invoke(app, ["links", "graph session"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "https://a.com" in result.output
    assert 'q="vectors"' in result.output
    assert "https://unfollowed.com" in result.output


def test_links_defaults_to_latest_session(tmp_path):
    _seed_links(tmp_path)
    result = runner.invoke(app, ["links"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "graph session" in result.output


def test_links_no_sessions(tmp_path):
    result = runner.invoke(app, ["links"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No sessions" in result.output
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_cli.py -v -k links`: FAIL (no `links` command).

- [ ] **Step 3: Edit `backend/src/browserbuddy/cli.py`**

Add a helper (after `_fmt_time`):

```python
def _resolve_session(rows: list, selector: str | None):
    """Pick a session row by id, exact name, or (default) the newest."""
    if selector is None:
        return rows[0] if rows else None
    if selector.isdigit():
        return next((r for r in rows if r["id"] == int(selector)), None)
    return next((r for r in rows if r["name"] == selector), None)
```

Add the command (after `sessions`):

```python
@app.command()
def links(
    session: str | None = typer.Argument(  # noqa: B008 — Typer requires Argument() in defaults
        None, help="Session id or name (default: newest)."
    ),
) -> None:
    """Show a session's navigation edges and notable un-followed outbound links."""
    conn = db.connect(get_settings().db_path)
    try:
        storage = Storage(conn)
        rows = storage.list_sessions(limit=200)
        target = _resolve_session(rows, session)
        if target is None:
            typer.echo("No matching session." if session else "No sessions recorded yet.")
            return
        edges = storage.list_nav_edges(target["id"])
        unfollowed = storage.list_unfollowed_links(target["id"])
    finally:
        conn.close()
    typer.echo(f"Session: {target['name']} (id {target['id']})")
    typer.echo("-- navigation edges --")
    if not edges:
        typer.echo("  (none)")
    for edge in edges:
        query = f'  q="{edge["query"]}"' if edge["query"] else ""
        origin = edge["from_url"] or "(start)"
        typer.echo(f"  {origin} → {edge['to_url']}  [{edge['transition'] or '?'}]{query}")
    typer.echo("-- notable un-followed links --")
    if not unfollowed:
        typer.echo("  (none)")
    for link in unfollowed:
        label = f"  — {link['anchor_text']}" if link["anchor_text"] else ""
        typer.echo(f"  {link['href']}{label}")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_cli.py -q`, then the full backend suite `uv run pytest -q`. Both green. `uv run ruff check . && uv run ruff format --check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/cli.py backend/tests/test_cli.py
git commit -m "Add buddy links command for the session link graph"
```

---

## Task 6: Extension — OutboundLink type

**Files:**
- Modify: `extension/src/types.ts`

- [ ] **Step 1: Add the `OutboundLink` interface to `extension/src/types.ts`** (near `PageCapture`):

```ts
export interface OutboundLink {
  href: string;
  text: string;
  rel: string;
}
```

The `links` field on `PageCapture` and the `link_click`/`nav_edge` union arms are added in Tasks 8 and 9, *alongside the code that produces them*, so every commit compiles cleanly.

- [ ] **Step 2: Run** — `npm --prefix extension run check`: clean (58 tests; a new exported interface with no behavior change and no consumers yet — tsc/eslint stay green).

- [ ] **Step 3: Commit**

```bash
git add extension/src/types.ts
git commit -m "Add OutboundLink protocol type"
```

---

## Task 7: Extension — outbound-link extraction

**Files:**
- Modify: `extension/src/extract.ts`
- Modify: `extension/tests/extract.test.ts`

- [ ] **Step 1: Write failing tests** — append to `extension/tests/extract.test.ts` (top of file already has `// @vitest-environment happy-dom`, `loadDocument`, and imports from `@src/extract`):

```ts
test("extractLinks returns deduped absolute http(s) links with text and rel", async () => {
  const { extractLinks } = await import("@src/extract");
  const doc = loadDocument(`
    <body>
      <a href="https://x.com/a">  First  </a>
      <a href="https://x.com/a">dup</a>
      <a href="https://y.com" rel="nofollow noopener">Y</a>
      <a href="mailto:hi@z.com">mail</a>
      <a>no href</a>
    </body>`);
  const links = extractLinks(doc);
  expect(links.map((l) => l.href)).toEqual(["https://x.com/a", "https://y.com"]);
  expect(links[0]!.text).toBe("First"); // whitespace collapsed + trimmed
  expect(links[1]!.rel).toContain("nofollow");
});

test("extractLinks caps anchor text length", async () => {
  const { extractLinks } = await import("@src/extract");
  const doc = loadDocument(`<body><a href="https://x.com">${"z".repeat(500)}</a></body>`);
  expect(extractLinks(doc)[0]!.text.length).toBe(300);
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- extract`: FAIL (`extractLinks` is not exported).

- [ ] **Step 3: Append to `extension/src/extract.ts`**

```ts
import type { OutboundLink } from "./types";

/** Catalogue a document's outbound links: absolute http(s) hrefs only, deduped,
 * with collapsed/capped anchor text. The content script runs this on every
 * captured page. Never throws. */
export function extractLinks(doc: Document): OutboundLink[] {
  const seen = new Set<string>();
  const out: OutboundLink[] = [];
  for (const anchor of Array.from(doc.links) as HTMLAnchorElement[]) {
    const href = anchor.href; // resolved to absolute by the DOM
    if (!/^https?:/i.test(href) || seen.has(href)) continue;
    seen.add(href);
    out.push({
      href,
      text: (anchor.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 300),
      rel: anchor.rel ?? "",
    });
  }
  return out;
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run check`: clean (`extractLinks` uses the `OutboundLink` type from Task 6; `capture.ts` is unchanged so tsc stays green).

- [ ] **Step 5: Commit**

```bash
git add extension/src/extract.ts extension/tests/extract.test.ts
git commit -m "Add outbound-link extraction"
```

---

## Task 8: Extension — capture conversion (links + click event)

**Files:**
- Modify: `extension/src/capture.ts`
- Modify: `extension/tests/capture.test.ts`

- [ ] **Step 1: Write failing tests** — in `extension/tests/capture.test.ts`, first update the existing top-of-file import (do NOT add a second `@src/capture` import line — eslint's `no-duplicate-imports` would reject it). Change `import { MAX_HTML_CHARS, pageCaptureEvent } from "@src/capture";` to:

```ts
import { MAX_HTML_CHARS, MAX_LINKS, linkClickEvent, pageCaptureEvent } from "@src/capture";
```

Then append these tests:

```ts
test("pageCaptureEvent carries outbound links, capped at MAX_LINKS", () => {
  const many = Array.from({ length: MAX_LINKS + 5 }, (_, i) => ({
    href: `https://x.com/${i}`, text: "t", rel: "",
  }));
  const event = pageCaptureEvent(msg({ links: many }), null, () => 1);
  expect(event!.links).toHaveLength(MAX_LINKS);
  expect(event!.links[0]!.href).toBe("https://x.com/0");
});

test("pageCaptureEvent defaults links to [] when absent", () => {
  const event = pageCaptureEvent(msg(), null, () => 1);
  expect(event!.links).toEqual([]);
});

test("linkClickEvent converts a bb:click message", () => {
  const event = linkClickEvent(
    { type: "bb:click", page_url: "https://a.com", href: "https://x.com", text: "X" },
    () => 1_000,
  );
  expect(event).toEqual({
    type: "link_click", page_url: "https://a.com", href: "https://x.com",
    text: "X", clicked_at: 1_000,
  });
});

test("linkClickEvent rejects messages missing page_url or href", () => {
  expect(linkClickEvent({ page_url: "", href: "https://x.com" }, () => 1)).toBeNull();
  expect(linkClickEvent({ page_url: "https://a.com", href: undefined }, () => 1)).toBeNull();
});
```

(The existing `msg()` helper at the top of the file builds a `bb:page` message; passing `{ links: … }` merges via its spread. Confirm `msg` spreads its argument — it does in the M3a test; if not, add `links` to the override object it already accepts.)

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- capture`: FAIL (`MAX_LINKS`/`linkClickEvent` missing; `links` not on the event).

- [ ] **Step 3a: Edit `extension/src/types.ts`** — add `links` to `PageCapture` and the `link_click` arm to `EventPayload` (the code below relies on both).

In the `PageCapture` interface, add a `links` field immediately after `container: string | null;`:

```ts
  container: string | null;
  links: OutboundLink[];
```

In the `EventPayload` union, replace the last arm line
`  | ({ type: "page_capture" } & PageCapture);`
with (note the terminating `;` moves to the new final arm):

```ts
  | ({ type: "page_capture" } & PageCapture)
  | {
      type: "link_click";
      page_url: string;
      href: string;
      text: string;
      clicked_at: number;
    };
```

- [ ] **Step 3b: Edit `extension/src/capture.ts`**

Add imports/const and extend `PageMessage`:

```ts
import type { EventPayload, OutboundLink } from "./types";

/** Cap on outbound links catalogued per page (bounds WS frame size alongside
 * the 2 MB HTML cap). */
export const MAX_LINKS = 1000;
```

Add `links` to `PageMessage`:

```ts
export interface PageMessage {
  type?: string;
  url?: string;
  title?: string;
  text?: string;
  html?: string;
  links?: OutboundLink[];
}
```

In `pageCaptureEvent`, add `links` to the returned object (after `container`):

```ts
    container,
    links: (message.links ?? []).slice(0, MAX_LINKS),
```

Append the click converter:

```ts
export interface ClickMessage {
  type?: string;
  page_url?: string;
  href?: string;
  text?: string;
}

/** Convert a content-script "bb:click" message into a link_click event.
 * Returns null when the page or target href is missing. */
export function linkClickEvent(
  message: ClickMessage,
  now: () => number = Date.now,
): (EventPayload & { type: "link_click" }) | null {
  if (!message.page_url || !message.href) return null;
  return {
    type: "link_click",
    page_url: message.page_url,
    href: message.href,
    text: message.text ?? "",
    clicked_at: now(),
  };
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- capture` (PASS) then the full gate `npm --prefix extension run check` (tsc now clean — `types.ts` declares `links`/`link_click` and `capture.ts` supplies them; all tests pass).

- [ ] **Step 5: Commit**

```bash
git add extension/src/types.ts extension/src/capture.ts extension/tests/capture.test.ts
git commit -m "Carry outbound links and convert link-click messages"
```

---

## Task 9: Extension — navigation module (search provenance + nav edge)

**Files:**
- Create: `extension/src/navigation.ts`
- Create: `extension/tests/navigation.test.ts`

- [ ] **Step 1: Write failing tests** — create `extension/tests/navigation.test.ts`:

```ts
import { expect, test } from "vitest";
import { navEdgeEvent, searchProvenance } from "@src/navigation";

test("searchProvenance extracts the query for known engines", () => {
  expect(searchProvenance("https://www.google.com/search?q=vector+db")).toBe("vector+db");
  expect(searchProvenance("https://duckduckgo.com/?q=hnsw")).toBe("hnsw");
  expect(searchProvenance("https://search.yahoo.com/search?p=faiss")).toBe("faiss");
  expect(searchProvenance("https://yandex.com/search/?text=annoy")).toBe("annoy");
});

test("searchProvenance returns null for non-search or malformed urls", () => {
  expect(searchProvenance("https://arxiv.org/abs/1")).toBeNull();
  expect(searchProvenance("https://www.google.com/maps")).toBeNull(); // no q
  expect(searchProvenance("not a url")).toBeNull();
  expect(searchProvenance(null)).toBeNull();
});

test("navEdgeEvent builds an event with joined qualifiers and derived query", () => {
  const event = navEdgeEvent({
    fromUrl: "https://www.bing.com/search?q=rag",
    toUrl: "https://a.com",
    transition: "link",
    qualifiers: ["client_redirect", "from_address_bar"],
    occurredAt: 42,
  });
  expect(event).toEqual({
    type: "nav_edge",
    from_url: "https://www.bing.com/search?q=rag",
    to_url: "https://a.com",
    transition: "link",
    qualifiers: "client_redirect,from_address_bar",
    query: "rag",
    occurred_at: 42,
  });
});

test("navEdgeEvent drops self-edges and missing targets", () => {
  expect(navEdgeEvent({ fromUrl: "https://a.com", toUrl: "https://a.com", occurredAt: 1 })).toBeNull();
  expect(navEdgeEvent({ fromUrl: null, toUrl: "", occurredAt: 1 })).toBeNull();
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- navigation`: FAIL (module missing).

- [ ] **Step 3a: Edit `extension/src/types.ts`** — add the `nav_edge` arm to `EventPayload`. Replace the closing of the `link_click` arm
```ts
      clicked_at: number;
    };
```
with (the terminating `;` moves to the new `nav_edge` arm):

```ts
      clicked_at: number;
    }
  | {
      type: "nav_edge";
      from_url: string | null;
      to_url: string;
      transition: string;
      qualifiers: string;
      query: string | null;
      occurred_at: number;
    };
```

- [ ] **Step 3b: Create `extension/src/navigation.ts`**

```ts
import type { EventPayload } from "./types";

// Known search engines → the query-string parameter carrying the user's query.
// Provenance is derived from the referring URL only — never by scraping result
// pages (design spec).
const SEARCH_ENGINES: Record<string, string> = {
  "www.google.com": "q",
  "google.com": "q",
  "www.bing.com": "q",
  "duckduckgo.com": "q",
  "search.brave.com": "q",
  "www.ecosia.org": "q",
  "www.startpage.com": "query",
  "search.yahoo.com": "p",
  "yandex.com": "text",
  "www.yandex.com": "text",
};

/** The search query that led to a page, or null when `fromUrl` is not a known
 * search engine (or is malformed). Never throws. */
export function searchProvenance(fromUrl: string | null): string | null {
  if (!fromUrl) return null;
  let url: URL;
  try {
    url = new URL(fromUrl);
  } catch {
    return null;
  }
  const param = SEARCH_ENGINES[url.hostname];
  if (!param) return null;
  const query = url.searchParams.get(param);
  return query && query.trim() ? query : null;
}

export interface NavEdgeInput {
  fromUrl: string | null;
  toUrl: string;
  transition?: string;
  qualifiers?: string[];
  occurredAt: number;
}

/** Build a nav_edge event, or null for a missing target or a self-edge
 * (reload / same-URL commit — not a navigation of interest). */
export function navEdgeEvent(
  input: NavEdgeInput,
): (EventPayload & { type: "nav_edge" }) | null {
  if (!input.toUrl || input.fromUrl === input.toUrl) return null;
  return {
    type: "nav_edge",
    from_url: input.fromUrl,
    to_url: input.toUrl,
    transition: input.transition ?? "",
    qualifiers: (input.qualifiers ?? []).join(","),
    query: searchProvenance(input.fromUrl),
    occurred_at: input.occurredAt,
  };
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- navigation`: PASS.

- [ ] **Step 5: Commit**

```bash
git add extension/src/types.ts extension/src/navigation.ts extension/tests/navigation.test.ts
git commit -m "Add search-provenance and nav-edge builder"
```

---

## Task 10: Extension — content script (link catalogue + click listener)

**Files:**
- Modify: `extension/src/content.ts`

Content script is a thin IIFE with no unit tests (verified by build + smoke, like M3a). Its pure logic (`extractLinks`) is already tested in Task 7.

- [ ] **Step 1: Replace `extension/src/content.ts`**

```ts
import { extractLinks, extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts the page + its
// outbound-link catalogue once the page settles, and reports every anchor
// click (including non-navigating ones the background's webNavigation cannot
// see: downloads, JS handlers, new-tab opens).
(() => {
  const runtime = (globalThis as { browser?: typeof browser }).browser?.runtime;

  const send = () => {
    const page = extractPage(document);
    const links = extractLinks(document);
    void runtime
      ?.sendMessage({ type: "bb:page", url: location.href, ...page, links })
      .catch(() => undefined); // background asleep/unreachable: drop silently
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

  if (document.readyState === "complete") {
    setTimeout(send, 500);
  } else {
    addEventListener("load", () => setTimeout(send, 500), { once: true });
  }
})();
```

- [ ] **Step 2: Run** — `npm --prefix extension run check` (still 60 tests, clean) and `npm --prefix extension run build`; confirm `dist/firefox/content.js` exists and is an IIFE (starts with `(function` / `var`), not an `import`.

- [ ] **Step 3: Commit**

```bash
git add extension/src/content.ts
git commit -m "Catalogue outbound links and report clicks from the content script"
```

---

## Task 11: Extension — background webNavigation wiring

**Files:**
- Modify: `extension/src/background.ts`

Background wiring; verified by build + smoke (pure helpers already tested in Task 9). The per-tab from-URL map lives in `storage.session` so it survives worker death.

- [ ] **Step 1: Edit `extension/src/background.ts`**

Add imports (next to the existing `capture`/`sessions` imports):

```ts
import { linkClickEvent, pageCaptureEvent, type ClickMessage, type PageMessage } from "./capture";
import { navEdgeEvent } from "./navigation";
```

(The existing import line is `import { pageCaptureEvent, type PageMessage } from "./capture";` — replace it with the line above.)

Add navigation plumbing (place after the `handlePageMessage` function):

```ts
const NAV_URLS_KEY = "bb:nav-urls"; // per-tab last committed URL, for edge from_url

async function tabUrls(): Promise<Record<number, string>> {
  return ((await session.get(NAV_URLS_KEY))[NAV_URLS_KEY] as Record<number, string>) ?? {};
}

async function rememberTabUrl(tabId: number, url: string): Promise<void> {
  const urls = await tabUrls();
  urls[tabId] = url;
  await session.set({ [NAV_URLS_KEY]: urls });
}

async function handleCommitted(
  details: { tabId: number; frameId: number; url: string; transitionType?: string; transitionQualifiers?: string[] },
): Promise<void> {
  if (details.frameId !== 0) return; // top-frame navigations only
  const fromUrl = (await tabUrls())[details.tabId] ?? null;
  await rememberTabUrl(details.tabId, details.url);
  const state = await sessionManager.current();
  if (state?.status !== "recording") return;
  const event = navEdgeEvent({
    fromUrl,
    toUrl: details.url,
    transition: details.transitionType,
    qualifiers: details.transitionQualifiers,
    occurredAt: Date.now(),
  });
  if (!event) return;
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
}

async function handleClickMessage(msg: ClickMessage): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status !== "recording") return;
  const event = linkClickEvent(msg);
  if (!event) return;
  await queue.enqueue(event);
  await (await ensureClient()).notifyEnqueued();
}
```

Register the `webNavigation` listeners (near the other `browser.*.addListener` calls):

```ts
browser.webNavigation.onCommitted.addListener((details) => {
  logged(handleCommitted(details));
});
// New-tab / new-window opens: seed the child tab's from-URL with the opener's
// current URL so the child's own onCommitted produces an opener→target edge.
browser.webNavigation.onCreatedNavigationTarget.addListener((details) => {
  logged(
    (async () => {
      const opener = (await tabUrls())[details.sourceTabId];
      if (opener) await rememberTabUrl(details.tabId, opener);
    })(),
  );
});
browser.tabs.onRemoved.addListener((tabId) => {
  logged(
    (async () => {
      const urls = await tabUrls();
      if (tabId in urls) {
        delete urls[tabId];
        await session.set({ [NAV_URLS_KEY]: urls });
      }
    })(),
  );
});
```

Handle `bb:click` in the existing `runtime.onMessage` listener — add this branch alongside the `bb:page` branch:

```ts
  if (msg?.type === "bb:click") {
    logged(handleClickMessage(message as ClickMessage));
    return false; // fire-and-forget
  }
```

> Note: `browser.tabs.onRemoved` may already have a listener in this file (M2 scheduled a snapshot on it). If so, ADD the URL-map cleanup inside the existing listener body instead of registering a second one — keep a single `onRemoved` listener. Verify by reading the file before editing.

- [ ] **Step 2: Run** — `npm --prefix extension run check` (60 tests, clean) and `npm --prefix extension run build`. If tsc complains that `browser.webNavigation` is untyped, the `@types/firefox-webext-browser` / `webextension-polyfill` types in use should already include it (the manifest gains the permission in Task 12); if a type error appears, confirm the permission/type package rather than casting.

- [ ] **Step 3: Commit**

```bash
git add extension/src/background.ts
git commit -m "Emit nav edges from webNavigation and handle link clicks"
```

---

## Task 12: Extension — manifest webNavigation permission

**Files:**
- Modify: `extension/manifest.firefox.json`, `extension/manifest.chrome.json`
- Modify: `extension/tests/manifest.test.ts`

- [ ] **Step 1: Write failing test** — append to `extension/tests/manifest.test.ts` (it already reads `firefoxOverlay`; add a chrome overlay read + a permission assertion):

```ts
const chromeOverlay = JSON.parse(
  readFileSync(new URL("../manifest.chrome.json", import.meta.url), "utf8"),
) as { permissions?: string[] };

test("webNavigation is granted on both targets (nav-edge capture)", () => {
  const ff = firefoxOverlay as unknown as { permissions?: string[] };
  expect(ff.permissions).toContain("webNavigation");
  expect(chromeOverlay.permissions).toContain("webNavigation");
});
```

(`firefoxOverlay`'s parse type doesn't include `permissions`; the `as unknown as` cast in the test keeps tsc happy without touching the top-of-file declaration.)

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- manifest`: FAIL (permission absent).

- [ ] **Step 3: Add the permission**

`manifest.firefox.json` — add `"webNavigation"` to the `permissions` array (becomes 9 entries).
`manifest.chrome.json` — add `"webNavigation"` to the `permissions` array (becomes 7 entries).

- [ ] **Step 4: Run** — `npm --prefix extension run test -- manifest` (PASS), then `npm --prefix extension run build`; confirm `dist/firefox/manifest.json` lists `webNavigation`.

- [ ] **Step 5: Commit**

```bash
git add extension/manifest.firefox.json extension/manifest.chrome.json extension/tests/manifest.test.ts
git commit -m "Grant webNavigation permission on both targets"
```

---

## Task 13: Gates, automated e2e, smoke instructions

**Files:** none (verification only)

- [ ] **Step 1: Full gates, both projects**

```bash
cd /Users/david/Projects/BrowserBuddy/extension && npm run check && npm run build && npx web-ext lint --source-dir dist/firefox
cd /Users/david/Projects/BrowserBuddy/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: extension clean (~64 tests: 58 baseline + link/click/nav/manifest additions); backend clean (M3a/Slice-0 baseline + Task 1–5 additions). `web-ext lint`: the pre-existing `data_collection_permissions` + bundled-Readability `UNSAFE_VAR_ASSIGNMENT` warnings are known/acceptable — report any new ones (a `webNavigation` permission notice, if any, is acceptable).

- [ ] **Step 2: Automated protocol e2e** (same pattern as prior milestones): scratch Node WS client against a temp-data-dir `buddy serve` on an unused port — auth, `session_start`, `page_capture` (with `links: [{href, text}]`), `nav_edge` (from a `google.com/search?q=…` URL → the page), `link_click` (on one catalogued href), expecting acks; then a `nav_edge` after `session_end` expecting an ERROR frame. Verify via `BROWSERBUDDY_DATA_DIR=… uv run buddy links` that the nav edge (with `q="…"`) and the un-followed link appear, and that the clicked link is absent from the un-followed set. Kill server, clean up. Report verbatim output.

- [ ] **Step 3: Update `README.md`** — under the research-sessions section, add one line:

```markdown
Inspect the link graph of a session: `buddy links "<session name>"` — shows how
you moved between pages (with the search query that led you there) and the
notable links you saw but didn't follow.
```

Commit:

```bash
cd /Users/david/Projects/BrowserBuddy
git add README.md
git commit -m "Document buddy links"
```

- [ ] **Step 4: Hand off the interactive smoke to the user** (not automatable): via `./scripts/smoke.sh`, pair, start a session, run a real web search (Google/DDG) and click through to a result, browse to a couple more pages following on-page links, click one link you then navigate away from and one you don't follow, Stop. Then `buddy links "<name>"` and confirm: (a) a nav edge from the search page with `q="…"`; (b) edges for the on-page link follows; (c) the un-followed link appears under "notable un-followed links"; (d) a followed link does NOT.

---

## Self-review notes

- **Spec coverage:** schema v3 `nav_edges`/`outbound_links` (Task 1); outbound-link catalogue via `page_capture.links` (Tasks 3,7,8,10) → `outbound_links` (Tasks 2,4); click stream (Tasks 8,10,11) → `mark_link_clicked` (Task 2,4); nav edges + provenance from `webNavigation`/referrer, not DOM scraping (Tasks 9,11,12); `buddy links` read surface (Task 5); `webNavigation` permission (Task 12). Deferred items (dwell/SPA/screenshots, embeddings/digests, REST blob) are out of scope and absent by design.
- **Type consistency:** `OutboundLink {href,text,rel}` matches across `models.py`, `types.ts`, `capture.ts`, `extract.ts`. `PageCaptureEvent.links` (py) ↔ `PageCapture.links` (ts). `NavEdgeEvent`/`nav_edge` fields (`from_url,to_url,transition,qualifiers,query,occurred_at`) match between `models.py`, `types.ts`, `navigation.ts`, `storage.add_nav_edge`, and `app.py`. `LinkClickEvent`/`link_click` fields (`page_url,href,text,clicked_at`) match across `models.py`, `types.ts`, `capture.linkClickEvent`, `app.py`, `storage.mark_link_clicked`. `add_page_visit` returns the id in Task 2 and Task 4 consumes it as `page_visit_id`.
- **Behavior preserved:** all three new ingest branches gate on an active session and raise `NoActiveSessionError` (caught by the WS loop → error frame, no ack — existing semantics). Additive protocol under v1. Dedup: outbound links UNIQUE(page,href); nav edges UNIQUE(session,from,to,occurred_at); page-visit id stable across duplicate captures.
- **Known simplifications (deliberate):** self-edges/reloads dropped (`navEdgeEvent` returns null when from==to); `from_url` NULL is tolerated (fresh tab / worker-death map loss) and, being NULL, is exempt from the edge UNIQUE dedup — acceptable at single-user volume; orphan clicks (page not captured) are dropped by `mark_link_clicked` returning False; provenance covers a fixed engine list (extend later).
```
