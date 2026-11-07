"""Compute off-thread, then atomically publish complete document generations."""

import struct

from browserbuddy.config import Settings
from browserbuddy.embeddings import INFERENCE, Chunk, LocalEmbedder, config_id, validate_vector
from browserbuddy.jobs import JobStore
from browserbuddy.memory import connection, content_version
from browserbuddy.memory_models import JobInfo


def publish_index(job: JobInfo, *, settings: Settings, chunks: list[Chunk], vectors: list) -> bool:
    """Publish only if both the claim and captured source still match.

    Args:
        job: Fenced running job.
        settings: Database location.
        chunks: Complete document chunks.
        vectors: Corresponding finite normalized vectors.
    """
    kind = job.kind.removeprefix("embed_")
    table = "page_visits" if kind == "page" else "history_entries"
    if len(chunks) != len(vectors) or not vectors:
        raise ValueError("all chunks require vectors")
    dimensions = len(vectors[0])
    vectors = [validate_vector(v, dimensions=dimensions) for v in vectors]
    with connection(settings) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            "SELECT 1 FROM enrichment_jobs WHERE id=? AND claim_token=? AND state='running'",
            (job.id, job.claim_token),
        ).fetchone()
        source = conn.execute(f"SELECT * FROM {table} WHERE id=?", (job.source_id,)).fetchone()
        if not claim or not source:
            return False
        version = content_version(
            source["title"], source["url"], source["text"] if kind == "page" else ""
        )
        if version != job.source_version:
            return False
        for chunk, vector in zip(chunks, vectors, strict=True):
            conn.execute(
                "INSERT OR REPLACE INTO memory_chunks(source_kind,source_id,source_version,"
                "config_id,ordinal,kind,text,start,end,vector) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    job.source_id,
                    version,
                    job.config_id,
                    chunk.ordinal,
                    chunk.kind,
                    chunk.text,
                    chunk.start,
                    chunk.end,
                    struct.pack(f"<{dimensions}f", *vector),
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO indexed_documents"
            "(source_kind,source_id,source_version,config_id,chunk_count) VALUES(?,?,?,?,?)",
            (kind, job.source_id, version, job.config_id, len(chunks)),
        )
        conn.execute(
            "UPDATE enrichment_jobs SET state='done',error_code=NULL WHERE id=?", (job.id,)
        )
    return True


def index_document(job: JobInfo, *, settings: Settings, embedder: LocalEmbedder) -> None:
    """Compute vectors outside transactions and publish a complete generation.

    Args:
        job: Claimed job with version and ownership metadata.
        settings: Backend configuration and storage location.
        embedder: Prepared local encoder boundary.
    """
    from browserbuddy.worker import CapabilityUnavailable, embedding_identity

    if not settings.semantic_enabled:
        raise CapabilityUnavailable("Semantic indexing disabled")
    if job.config_id != embedding_identity(settings):
        JobStore(settings).finish(job.id, claim_token=job.claim_token)
        return
    table = "page_visits" if job.kind == "embed_page" else "history_entries"
    with connection(settings) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (job.source_id,)).fetchone()
    if row is None:
        raise ValueError("source missing")
    encoded = INFERENCE.submit(_encode_generation, embedder, job, dict(row)).result()
    if encoded is None:
        JobStore(settings).finish(job.id, claim_token=job.claim_token)
        return
    chunks, vectors = encoded
    if not publish_index(job, settings=settings, chunks=chunks, vectors=vectors):
        JobStore(settings).finish(job.id, claim_token=job.claim_token)


def _encode_generation(embedder, job, row):
    embedder.load()
    if config_id(embedder.config) != job.config_id:
        return None
    return embedder.document(
        title=row["title"], url=row["url"], text=row["text"] if job.kind == "embed_page" else ""
    )
