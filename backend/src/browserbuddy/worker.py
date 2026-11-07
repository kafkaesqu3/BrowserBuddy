"""Discover committed sources without coupling inference to ingestion."""

import asyncio
import logging
import time

from browserbuddy.config import Settings
from browserbuddy.jobs import JobKey, JobStore
from browserbuddy.memory import connection, content_version

logger = logging.getLogger(__name__)


class CapabilityUnavailable(RuntimeError):
    """Work may resume when a local capability is configured or prepared."""


class Worker:
    def __init__(self, settings: Settings, *, handler=None):
        self.settings = settings
        self.handler = handler or self._handle
        self.jobs = JobStore(settings)
        self.tasks = []

    def start(self) -> None:
        self.jobs.recover()
        self.tasks = [asyncio.create_task(self._discover()), asyncio.create_task(self._run())]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await asyncio.to_thread(self.jobs.recover)

    async def _discover(self) -> None:
        while True:
            try:
                await asyncio.to_thread(reconcile, self.settings, now=int(time.time() * 1000))
            except Exception as exc:
                logger.error("Reconciliation failed (%s); will retry", type(exc).__name__)
            await asyncio.sleep(60)

    async def _run(self) -> None:
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception as exc:
                logger.error("Job runner unavailable (%s); will retry", type(exc).__name__)
            await asyncio.sleep(1)

    async def run_once(self) -> bool:
        now = int(time.time() * 1000)
        job = await asyncio.to_thread(self.jobs.claim, now=now)
        if job is None:
            return False
        try:
            await self.handler(job)
        except CapabilityUnavailable:
            await asyncio.to_thread(self._defer, job, now)
        except Exception as exc:
            await asyncio.to_thread(
                self.jobs.fail,
                job.id,
                claim_token=job.claim_token,
                error_code=type(exc).__name__,
                transient=not isinstance(exc, ValueError),
                now=now,
            )
        return True

    def _defer(self, job, now):
        with connection(self.settings) as conn, conn:
            conn.execute(
                "UPDATE enrichment_jobs SET state='pending',attempt=MAX(0,attempt-1),"
                "next_attempt_at=?,claim_token=NULL WHERE id=? AND claim_token=?",
                (now + 60000, job.id, job.claim_token),
            )

    async def _handle(self, job):
        raise CapabilityUnavailable("No handler configured")


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
