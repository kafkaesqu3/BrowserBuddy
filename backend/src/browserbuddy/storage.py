import sqlite3

from browserbuddy.models import HistoryVisit, TabRecord


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
        """Record a captured page. Returns rows inserted (0 on duplicate).

        Raises NoActiveSessionError for an unknown session_id so the WS loop
        can send an error frame instead of dying.
        """
        # Exists-check outside the txn is fine: sessions are never deleted in
        # v2; the FK constraint is the backstop.
        exists = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            raise NoActiveSessionError(f"no such session: {session_id}")
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
