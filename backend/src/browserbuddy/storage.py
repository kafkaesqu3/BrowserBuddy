import sqlite3

from browserbuddy.models import HistoryVisit, TabRecord


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
