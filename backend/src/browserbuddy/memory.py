"""Owned-connection retrieval over captured evidence, independent of inference."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from browserbuddy.config import Settings
from browserbuddy.memory_models import (
    Capabilities,
    Citation,
    DigestResult,
    OpenTab,
    PageEvidence,
    ScreenshotInfo,
    ScreenshotPage,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SessionInfo,
    SessionPage,
)


@contextmanager
def connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Open an operation-owned connection to an already migrated database.

    Args:
        settings: Backend database location.
    """
    conn = sqlite3.connect(settings.db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def content_version(title: str, url: str, text: str = "") -> str:
    return hashlib.sha256(json.dumps([title, url, text], ensure_ascii=False).encode()).hexdigest()


def page_bounds(after_id: int, limit: int) -> None:
    if after_id < 0 or not 1 <= limit <= 100:
        raise ValueError("after_id must be nonnegative and limit must be 1..100")


def filtered_source(request: SearchRequest, kind: str) -> tuple[str, list, str, str]:
    if kind == "history" and request.session_id is not None:
        raise ValueError("history search cannot filter by session")
    table, timestamp = (
        ("page_visits", "visited_at") if kind == "page" else ("history_entries", "visit_time")
    )
    clauses, params = [], []
    for operator, value in ((">=", request.since), ("<", request.until)):
        if value is not None:
            clauses.append(f"p.{timestamp} {operator} ?")
            params.append(value)
    if request.session_id is not None:
        clauses.append("p.session_id = ?")
        params.append(request.session_id)
    return " AND ".join(clauses) or "1", params, table, timestamp


def lexical_hits(settings: Settings, request: SearchRequest, kind: str) -> list[SearchHit]:
    where, params, table, timestamp = filtered_source(request, kind)
    if not request.query:
        return []
    fts = "page_fts" if kind == "page" else "history_fts"
    query = " OR ".join('"' + word.replace('"', '""') + '"' for word in request.query.split())
    extra = (
        "p.text, p.session_id, s.name AS session_name"
        if kind == "page"
        else ("'' AS text, NULL AS session_id, NULL AS session_name")
    )
    join = "JOIN sessions s ON s.id = p.session_id" if kind == "page" else ""
    with connection(settings) as conn:
        rows = conn.execute(
            f"SELECT p.id, p.url, p.title, p.{timestamp} AS captured_at, {extra},"
            f" bm25({fts}) AS score FROM {fts} JOIN {table} p ON p.id = {fts}.rowid {join}"
            f" WHERE {fts} MATCH ? AND {where} ORDER BY score, captured_at DESC, p.id LIMIT 201",
            [query, *params],
        ).fetchall()
    return [row_hit(row, kind) for row in rows]


def row_hit(row: sqlite3.Row, kind: str) -> SearchHit:
    return SearchHit(
        source_kind=kind,
        source_id=row["id"],
        session_id=row["session_id"],
        session_name=row["session_name"],
        url=row["url"],
        title=row["title"],
        captured_at=row["captured_at"],
        excerpt=(row["text"] or row["title"])[:1200],
        citation=Citation(source_kind=kind, source_id=row["id"]),
        score=row["score"],
        content_hash=content_version(row["title"], row["url"], row["text"]),
    )


def collapse_hits(hits: list[SearchHit]) -> list[SearchHit]:
    found = {}
    for hit in hits:
        key = (
            (hit.session_id, hit.url, hit.content_hash)
            if hit.source_kind == "page"
            else ("history", hit.source_id)
        )
        if key in found:
            found[key].collapsed_count += 1
        else:
            found[key] = hit.model_copy()
    return list(found.values())


class MemoryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def search_memory(self, request: SearchRequest) -> SearchResponse:
        return self._search(request, "page")

    def search_history(self, request: SearchRequest) -> SearchResponse:
        return self._search(request, "history")

    def _search(self, request: SearchRequest, kind: str) -> SearchResponse:
        hits = lexical_hits(self.settings, request, kind)
        capped = len(hits) > 200
        collapsed = collapse_hits(hits[:200])
        return SearchResponse(
            hits=collapsed[: request.limit],
            truncated=capped or len(collapsed) > request.limit,
            capabilities=self.capabilities(),
            warnings=["candidate_cap_reached"] if capped else [],
        )

    def get_page(self, page_visit_id: int, *, offset: int = 0, max_chars: int = 12000):
        if offset < 0 or not 1 <= max_chars <= 24000:
            raise ValueError("offset must be nonnegative and max_chars must be 1..24000")
        with connection(self.settings) as conn:
            row = conn.execute(
                "SELECT p.*, s.name AS session_name FROM page_visits p"
                " JOIN sessions s ON s.id=p.session_id WHERE p.id=?",
                (page_visit_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"page {page_visit_id} not found")
        end = offset + max_chars
        return PageEvidence(
            **{
                k: row[k]
                for k in (
                    "id",
                    "session_id",
                    "session_name",
                    "url",
                    "title",
                    "visited_at",
                    "dwell_ms",
                )
            },
            text=row["text"][offset:end],
            offset=offset,
            next_offset=end if end < len(row["text"]) else None,
            truncated=end < len(row["text"]),
            html_available=self._blob_exists(row["html_key"]),
        )

    def _blob_exists(self, key: str | None) -> bool:
        if key is None or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            return False
        base = self.settings.data_dir / "blobs" / key[:2] / key[2:]
        return base.with_suffix(".bin").is_file() or base.with_suffix(".gz").is_file()

    def list_sessions(self, *, after_id: int = 0, limit: int = 20) -> SessionPage:
        page_bounds(after_id, limit)
        with connection(self.settings) as conn:
            rows = conn.execute(
                "SELECT s.id,s.name,s.started_at,s.ended_at,COUNT(p.id) AS page_count"
                " FROM sessions s LEFT JOIN page_visits p ON p.session_id=s.id"
                " WHERE s.id>? GROUP BY s.id ORDER BY s.id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
        return SessionPage(
            items=[SessionInfo(**dict(row)) for row in rows[:limit]],
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
            truncated=len(rows) > limit,
        )

    def get_open_tabs(self) -> list[OpenTab]:
        with connection(self.settings) as conn:
            return [
                OpenTab(**dict(row))
                for row in conn.execute("SELECT * FROM open_tabs ORDER BY window_id,tab_id")
            ]

    def get_screenshots(self, page_visit_id: int, *, after_id=0, limit=20) -> ScreenshotPage:
        page_bounds(after_id, limit)
        self.get_page(page_visit_id, max_chars=1)
        with connection(self.settings) as conn:
            rows = conn.execute(
                "SELECT id,page_visit_id,captured_at,kind,blob_key FROM screenshots"
                " WHERE page_visit_id=? AND id>? ORDER BY id LIMIT ?",
                (page_visit_id, after_id, limit + 1),
            ).fetchall()
        return ScreenshotPage(
            items=[
                ScreenshotInfo(**dict(row), available=self._blob_exists(row["blob_key"]))
                for row in rows[:limit]
            ],
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
            truncated=len(rows) > limit,
        )

    def get_session_digest(self, session_id: int) -> DigestResult:
        with connection(self.settings) as conn:
            if not conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise LookupError(f"session {session_id} not found")
        return DigestResult()
