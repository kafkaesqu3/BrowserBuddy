"""Discover committed sources without coupling inference to ingestion."""

from browserbuddy.config import Settings
from browserbuddy.jobs import JobKey, JobStore
from browserbuddy.memory import connection, content_version


def embedding_identity(settings: Settings) -> str:
    return content_version(settings.embedding_model, settings.embedding_revision, "512/64/32/v1")


def reconcile(settings: Settings, *, now: int) -> int:
    """Recover missing jobs from committed source rows in bounded keyset batches.

    Args:
        settings: Backend capabilities and storage location.
        now: Sweep time; source identity deliberately excludes wall-clock time.
    """
    if not settings.semantic_enabled:
        return 0
    jobs, added = JobStore(settings), 0
    config = embedding_identity(settings)
    for kind, table in (("page", "page_visits"), ("history", "history_entries")):
        after = 0
        while True:
            with connection(settings) as conn:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT 200", (after,)
                ).fetchall()
            if not rows:
                break
            for row in rows:
                version = content_version(
                    row["title"], row["url"], row["text"] if kind == "page" else ""
                )
                key = JobKey(f"embed_{kind}", row["id"], version, config)
                with connection(settings) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM enrichment_jobs WHERE kind=? AND"
                        " source_id=? AND source_version=? AND config_id=?",
                        (key.kind, key.source_id, version, config),
                    ).fetchone()
                jobs.enqueue(key)
                added += exists is None
            after = rows[-1]["id"]
    return added
