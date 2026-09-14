import sqlite3

from browserbuddy.models import HistoryVisit, OutboundLink, TabRecord
from browserbuddy.phash import hamming_distance


class NoActiveSessionError(LookupError):
    """A page capture arrived with no active session (client-side race)."""


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
        """Full-text search over history.

        Tokens are OR-combined and bm25-ranked, so any matching term returns a
        result and pages matching more terms rank higher (recall-friendly for
        short title/URL fields). Each token is quoted, so FTS5 operators in user
        input are treated as literals, not syntax.
        """
        limit = max(1, min(limit, 200))
        tokens = query.split()
        if not tokens:
            return []
        fts_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
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
            "SELECT tab_id, window_id, url, title, group_id, container,"
            " opened_at, last_activated_at, active_ms"
            " FROM open_tabs ORDER BY window_id, tab_id"
        ).fetchall()

    # -- ingest client cursors --------------------------------------------

    def get_last_seq(self, client_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_seq FROM ingest_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return row["last_seq"] if row else 0

    def set_last_seq(self, client_id: str, seq: int) -> None:
        """Advance the client's ingest cursor.

        Monotonic: the cursor never regresses, so a stale concurrent connection
        cannot rewind acked progress.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO ingest_clients (client_id, last_seq) VALUES (?, ?)"
                " ON CONFLICT (client_id)"
                " DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq)",
                (client_id, seq),
            )

    # -- sessions ----------------------------------------------------------

    def start_session(self, client_id: str, name: str, started_at: int) -> int:
        """Open a session; any still-active session for this client is
        implicitly closed at the new start time (one active session per
        client by design). Idempotent on (client_id, started_at): a replayed
        or same-millisecond start returns the existing session instead of
        stranding the event in an IntegrityError retry loop.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?"
                " WHERE client_id = ? AND ended_at IS NULL AND started_at != ?",
                (started_at, client_id, started_at),
            )
            self._conn.execute(
                "INSERT INTO sessions (client_id, name, started_at) VALUES (?, ?, ?)"
                " ON CONFLICT (client_id, started_at) DO NOTHING",
                (client_id, name, started_at),
            )
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE client_id = ? AND started_at = ?",
            (client_id, started_at),
        ).fetchone()
        return row["id"]

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
        """Record a captured page, returning its page_visit id.

        Returns the existing id on a duplicate so outbound links still attach.
        Raises NoActiveSessionError for an unknown session_id so the WS loop
        can send an error frame instead of dying.
        """
        # Exists-check outside the txn is fine: sessions are never deleted in
        # v2; the FK constraint is the backstop.
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

    def list_sessions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT s.id, s.client_id, s.name, s.started_at, s.ended_at,
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
            SELECT p.url, p.title, p.visited_at, p.dwell_ms, s.name AS session_name
            FROM page_fts f
            JOIN page_visits p ON p.id = f.rowid
            JOIN sessions s ON s.id = p.session_id
            WHERE page_fts MATCH ?
            ORDER BY bm25(page_fts), p.visited_at DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()

    # -- link graph --------------------------------------------------------

    def add_outbound_links(self, page_visit_id: int, links: list[OutboundLink]) -> int:
        """Catalogue a page's outbound links. Duplicate (page, href) ignored."""
        with self._conn:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO outbound_links"
                " (page_visit_id, href, anchor_text, rel, in_main)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (page_visit_id, link.href, link.text, link.rel, int(link.in_main))
                    for link in links
                ],
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

    def list_nav_edges(self, session_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT from_url, to_url, transition, qualifiers, query, occurred_at"
            " FROM nav_edges WHERE session_id = ? ORDER BY occurred_at, id LIMIT ?",
            (session_id, limit),
        ).fetchall()

    def list_unfollowed_links(self, session_id: int, limit: int = 50) -> list[sqlite3.Row]:
        """Outbound links seen but not clicked, for the digest's 'notable
        un-followed links'. Restricted to main-content links (in_main = 1) so the
        digest surfaces article links, not site chrome; the full catalogue is
        still stored for later ranking/analysis."""
        return self._conn.execute(
            "SELECT o.href, o.anchor_text, p.url AS page_url"
            " FROM outbound_links o JOIN page_visits p ON p.id = o.page_visit_id"
            " WHERE p.session_id = ? AND o.clicked = 0 AND o.in_main = 1"
            " ORDER BY p.visited_at, o.id LIMIT ?",
            (session_id, limit),
        ).fetchall()

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

    def add_page_screenshot(
        self,
        session_id: int,
        *,
        url: str,
        occurred_at: int,
        kind: str,
        blob_key: str,
        phash: str,
        dedup_threshold: int,
    ) -> int | None:
        """Insert a screenshots row for the latest page_visit at url in this
        session, unless it's a near-duplicate (Hamming distance <=
        dedup_threshold) of that page_visit's most recently stored
        screenshot. Returns the new row id, or None when the page was never
        captured (orphan, same convention as mark_link_clicked/
        add_page_dwell) or the capture was deduped away.
        """
        row = self._conn.execute(
            "SELECT id FROM page_visits WHERE session_id = ? AND url = ?"
            " ORDER BY visited_at DESC LIMIT 1",
            (session_id, url),
        ).fetchone()
        if row is None:
            return None
        page_visit_id = row["id"]
        latest = self._conn.execute(
            "SELECT phash FROM screenshots WHERE page_visit_id = ?"
            " ORDER BY captured_at DESC, id DESC LIMIT 1",
            (page_visit_id,),
        ).fetchone()
        if latest is not None and hamming_distance(latest["phash"], phash) <= dedup_threshold:
            return None
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO screenshots (page_visit_id, blob_key, captured_at, kind, phash)"
                " VALUES (?, ?, ?, ?, ?)",
                (page_visit_id, blob_key, occurred_at, kind, phash),
            )
        return cur.lastrowid

    def list_screenshots(self, session_id: int, url: str | None = None) -> list[sqlite3.Row]:
        query = (
            "SELECT sc.id, sc.blob_key, sc.captured_at, sc.kind, p.url"
            " FROM screenshots sc JOIN page_visits p ON p.id = sc.page_visit_id"
            " WHERE p.session_id = ?"
        )
        params: list[int | str] = [session_id]
        if url is not None:
            query += " AND p.url = ?"
            params.append(url)
        query += " ORDER BY sc.captured_at, sc.id"
        return self._conn.execute(query, params).fetchall()
