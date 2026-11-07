"""Filtered exact vector retrieval and deterministic reciprocal rank fusion."""

import sqlite3
import struct

from browserbuddy.embeddings import prepared_config, validate_vector
from browserbuddy.memory import (
    collapse_hits,
    connection,
    content_version,
    filtered_source,
    lexical_hits,
    row_hit,
)
from browserbuddy.memory_models import Capabilities, SearchHit, SearchRequest, SearchResponse
from browserbuddy.worker import CapabilityUnavailable, embedding_identity


def fuse_hits(lexical: list[SearchHit], semantic: list[SearchHit]) -> list[SearchHit]:
    scores, evidence = {}, {}
    for branch in (lexical, semantic):
        for rank, hit in enumerate(branch, 1):
            key = (hit.source_kind, hit.source_id)
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            evidence[key] = hit
    results = [hit.model_copy(update={"score": scores[key]}) for key, hit in evidence.items()]
    return sorted(results, key=lambda h: (-h.score, -h.captured_at, h.source_id))


class HybridSearch:
    def __init__(self, settings, embedder):
        self.settings, self.embedder = settings, embedder

    def capabilities(self) -> Capabilities:
        """Report configured generation coverage and local availability."""
        caps = Capabilities(llm_configured=self.settings.llm_configured)
        if not self.settings.semantic_enabled:
            return caps
        with connection(self.settings) as conn:
            caps.eligible = conn.execute(
                "SELECT (SELECT COUNT(*) FROM page_visits) + (SELECT COUNT(*) FROM history_entries)"
            ).fetchone()[0]
            caps.indexed = conn.execute(
                "SELECT COUNT(*) FROM indexed_documents WHERE config_id=?",
                (embedding_identity(self.settings),),
            ).fetchone()[0]
        caps.semantic = "indexing" if caps.indexed < caps.eligible else "ready"
        if prepared_config(self.settings) is None:
            caps.semantic = "unavailable"
        return caps

    def search(self, request: SearchRequest, *, source_kind: str) -> SearchResponse:
        """Fuse filtered retrieval branches or report lexical fallback.

        Args:
            request: Validated query and caller-selected filters.
            source_kind: Page or history retrieval source.
        """
        lexical = lexical_hits(self.settings, request, source_kind)
        capped, mode, warnings = len(lexical) > 200, "fts", []
        hits = lexical[:200]
        if self.settings.semantic_enabled and request.query:
            try:
                semantic = self._vectors(request, source_kind)
                capped |= len(semantic) > 200
                hits = fuse_hits(hits, semantic[:200])
                mode = "hybrid"
            except (
                ImportError,
                OSError,
                ValueError,
                TimeoutError,
                CapabilityUnavailable,
                sqlite3.Error,
            ):
                warnings.append("semantic_unavailable")
        hits = collapse_hits(hits)
        capabilities = self.capabilities()
        if "semantic_unavailable" in warnings:
            capabilities.semantic = "unavailable"
        if capped:
            warnings.append("candidate_cap_reached")
        return SearchResponse(
            hits=hits[: request.limit],
            truncated=capped or len(hits) > request.limit,
            mode=mode,
            capabilities=capabilities,
            warnings=warnings,
        )

    def _vectors(self, request: SearchRequest, kind: str) -> list[SearchHit]:
        import sqlite_vec

        generation = embedding_identity(self.settings)
        vector = self.embedder.query(request.query)
        if embedding_identity(self.settings) != generation:
            raise CapabilityUnavailable("Embedding generation changed during query")
        vector = validate_vector(vector, dimensions=len(vector))
        packed = struct.pack(f"<{len(vector)}f", *vector)
        where, params, table, timestamp = filtered_source(request, kind)
        extra = (
            "p.text,p.session_id,s.name AS session_name"
            if kind == "page"
            else ("'' AS text,NULL AS session_id,NULL AS session_name")
        )
        join = "JOIN sessions s ON s.id=p.session_id" if kind == "page" else ""
        text = "p.text" if kind == "page" else "''"
        with connection(self.settings) as conn:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            conn.create_function("content_version", 3, content_version, deterministic=True)
            rows = conn.execute(
                f"WITH distances AS (SELECT p.id,p.url,p.title,p.{timestamp} AS captured_at,"
                f"{extra},c.id AS chunk_id,c.start,c.end,c.text AS excerpt,"
                "vec_distance_cosine(c.vector,?) AS score,"
                "ROW_NUMBER() OVER(PARTITION BY p.id ORDER BY vec_distance_cosine(c.vector,?),"
                "c.id) AS rn FROM memory_chunks c"
                f" JOIN {table} p ON p.id=c.source_id {join}"
                " JOIN indexed_documents d ON d.source_kind=c.source_kind"
                " AND d.source_id=c.source_id AND d.source_version=c.source_version"
                " AND d.config_id=c.config_id WHERE c.config_id=? AND c.source_kind=?"
                f" AND d.source_version=content_version(p.title,p.url,{text}) AND {where})"
                " SELECT * FROM distances WHERE rn=1 ORDER BY score,captured_at DESC,id LIMIT 201",
                [packed, packed, generation, kind, *params],
            ).fetchall()
        hits = []
        for row in rows:
            hit = row_hit(row, kind)
            hit.chunk_id = hit.citation.chunk_id = row["chunk_id"]
            hit.citation.start, hit.citation.end = row["start"], row["end"]
            hit.excerpt = row["excerpt"][:1200]
            if hit.citation.start is not None:
                hit.citation.end = hit.citation.start + len(hit.excerpt)
            hits.append(hit)
        return hits
