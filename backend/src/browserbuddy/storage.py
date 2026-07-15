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
