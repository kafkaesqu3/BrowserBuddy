"""Persistent, claim-fenced enrichment work."""

import uuid
from dataclasses import dataclass

from browserbuddy.config import Settings
from browserbuddy.memory import connection, page_bounds
from browserbuddy.memory_models import JobInfo, JobPage


@dataclass(frozen=True)
class JobKey:
    kind: str
    source_id: int
    source_version: str
    config_id: str


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def enqueue(self, key: JobKey, *, origin: str = "automatic") -> JobInfo:
        """Persist work once per source and configuration identity.

        Args:
            key: Unique source/version/configuration identity.
            origin: Explicit or automatic request origin.
        """
        with connection(self.settings) as conn, conn:
            conn.execute(
                "INSERT INTO enrichment_jobs(kind,source_id,source_version,config_id,origin)"
                " VALUES(?,?,?,?,?) ON CONFLICT(kind,source_id,source_version,config_id)"
                " DO UPDATE SET origin=CASE WHEN excluded.origin='explicit' THEN 'explicit'"
                " ELSE enrichment_jobs.origin END",
                (key.kind, key.source_id, key.source_version, key.config_id, origin),
            )
            row = conn.execute(
                "SELECT * FROM enrichment_jobs WHERE kind=? AND source_id=?"
                " AND source_version=? AND config_id=?",
                (key.kind, key.source_id, key.source_version, key.config_id),
            ).fetchone()
            return JobInfo(**dict(row))

    def get(self, job_id: int) -> JobInfo:
        with connection(self.settings) as conn:
            row = conn.execute("SELECT * FROM enrichment_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"job {job_id} not found")
        return JobInfo(**dict(row))

    def list(self, *, after_id: int = 0, limit: int = 20) -> JobPage:
        """List durable jobs and their retry status.

        Args:
            after_id: Exclusive ID cursor.
            limit: Maximum returned items.
        """
        page_bounds(after_id, limit)
        with connection(self.settings) as conn:
            rows = conn.execute(
                "SELECT * FROM enrichment_jobs WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
        return JobPage(
            items=[JobInfo(**dict(r)) for r in rows[:limit]],
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
            truncated=len(rows) > limit,
        )

    def claim(self, *, now: int) -> JobInfo | None:
        """Atomically claim eligible work using a unique publication token.

        Args:
            now: Current time used for deterministic scheduling or calendar resolution.
        """
        with connection(self.settings) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM enrichment_jobs WHERE state='pending' AND next_attempt_at<=?"
                " AND (kind!='digest' OR origin='explicit' OR ?) ORDER BY id LIMIT 1",
                (now, self.settings.auto_digests),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE enrichment_jobs SET state='running',attempt=attempt+1,"
                "claim_token=? WHERE id=?",
                (uuid.uuid4().hex, row["id"]),
            )
            return JobInfo(
                **dict(
                    conn.execute(
                        "SELECT * FROM enrichment_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
                )
            )

    def finish(self, job_id: int, *, claim_token: str) -> bool:
        with connection(self.settings) as conn, conn:
            return (
                conn.execute(
                    "UPDATE enrichment_jobs SET state='done',error_code=NULL"
                    " WHERE id=? AND state='running' AND claim_token=?",
                    (job_id, claim_token),
                ).rowcount
                == 1
            )

    def fail(
        self, job_id: int, *, claim_token: str, error_code: str, transient: bool, now: int
    ) -> None:
        """Record a fenced failure and schedule bounded retries.

        Args:
            job_id: Persistent job identifier.
            claim_token: Token proving ownership of this attempt.
            error_code: Sanitized failure code.
            transient: Whether the failure is eligible for retry.
            now: Current time used for deterministic scheduling or calendar resolution.
        """
        job = self.get(job_id)
        retry = transient and job.attempt < 6
        delay = [5000, 30000, 120000, 600000, 3600000][job.attempt - 1] if retry else 0
        with connection(self.settings) as conn, conn:
            conn.execute(
                "UPDATE enrichment_jobs SET state=?,next_attempt_at=?,error_code=?,"
                "claim_token=NULL WHERE id=? AND state='running' AND claim_token=?",
                ("pending" if retry else "failed", now + delay, error_code, job_id, claim_token),
            )

    def retry(self, job_id: int) -> JobInfo:
        """Explicitly reset a failed job without duplicating its identity.

        Args:
            job_id: Persistent job identifier.
        """
        self.get(job_id)
        with connection(self.settings) as conn, conn:
            changed = conn.execute(
                "UPDATE enrichment_jobs SET state='pending',attempt=0,"
                "next_attempt_at=0,claim_token=NULL,error_code=NULL"
                " WHERE id=? AND state='failed'",
                (job_id,),
            ).rowcount
            if not changed:
                raise ValueError("only failed jobs may be retried")
        return self.get(job_id)

    def recover(self) -> None:
        with connection(self.settings) as conn, conn:
            conn.execute(
                "UPDATE enrichment_jobs SET state='pending',claim_token=NULL,"
                "attempt=MAX(0,attempt-1) WHERE state='running'"
            )
