"""Immutable session evidence and durable digest lifecycle."""

import hashlib
import json

from pydantic import BaseModel

from browserbuddy.config import Settings
from browserbuddy.memory import connection
from browserbuddy.memory_models import DigestResult


class DigestManifest(BaseModel):
    session: dict
    pages: list[dict]
    links: list[dict]
    navigation: list[dict]
    capture_cutoff: int

    @property
    def source_version(self) -> str:
        return fingerprint(self.model_dump())


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def digest_config(settings: Settings) -> str:
    return fingerprint(
        {"model": settings.llm_model, "endpoint": settings.llm_base_url, "prompt_version": 1}
    )


def snapshot_session(session_id: int, *, settings: Settings) -> DigestManifest:
    """Copy all generation inputs in one consistent SQLite read snapshot.

    Args:
        session_id: Existing session, including an active session.
        settings: Database location.
    """
    with connection(settings) as conn, conn:
        conn.execute("BEGIN")
        session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise LookupError(f"session {session_id} not found")
        pages = [
            dict(row)
            for row in conn.execute(
                "SELECT id,session_id,url,title,visited_at,text,dwell_ms FROM page_visits"
                " WHERE session_id=? ORDER BY visited_at,id",
                (session_id,),
            )
        ]
        links = [
            dict(row)
            for row in conn.execute(
                "SELECT o.*,p.url AS page_url,NOT EXISTS(SELECT 1 FROM history_entries h"
                " WHERE h.url=o.href AND h.visit_time<?) AND NOT EXISTS(SELECT 1 FROM page_visits v"
                " WHERE v.url=o.href AND v.visited_at<?) AS novel FROM outbound_links o"
                " JOIN page_visits p ON p.id=o.page_visit_id WHERE p.session_id=? ORDER BY o.id",
                (session["started_at"], session["started_at"], session_id),
            )
        ]
        navigation = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM nav_edges WHERE session_id=? ORDER BY occurred_at,id", (session_id,)
            )
        ]
    return DigestManifest(
        session=dict(session),
        pages=pages,
        links=links,
        navigation=navigation,
        capture_cutoff=max((p["visited_at"] for p in pages), default=session["started_at"]),
    )


class DigestService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def request_digest(
        self, session_id: int, *, regenerate=False, origin="explicit"
    ) -> DigestResult:
        manifest = snapshot_session(session_id, settings=self.settings)
        version, config = manifest.source_version, digest_config(self.settings)
        with connection(self.settings) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO enrichment_jobs(kind,source_id,source_version,config_id,origin)"
                " VALUES('digest',?,?,?,?) ON CONFLICT(kind,source_id,source_version,config_id)"
                " DO UPDATE SET origin=CASE WHEN excluded.origin='explicit' THEN 'explicit'"
                " ELSE enrichment_jobs.origin END",
                (session_id, version, config, origin),
            )
            job = conn.execute(
                "SELECT * FROM enrichment_jobs WHERE kind='digest' AND source_id=?"
                " AND source_version=? AND config_id=?",
                (session_id, version, config),
            ).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO digests(session_id,source_version,config_id,job_id,"
                "evidence_manifest) VALUES(?,?,?,?,?)",
                (session_id, version, config, job["id"], manifest.model_dump_json()),
            )
            if regenerate and job["state"] in ("done", "failed"):
                conn.execute(
                    "UPDATE enrichment_jobs SET state='pending',attempt=0,next_attempt_at=0,"
                    "claim_token=NULL,error_code=NULL,origin='explicit' WHERE id=?",
                    (job["id"],),
                )
                conn.execute(
                    "UPDATE digests SET summary_checkpoint='{}' WHERE job_id=?", (job["id"],)
                )
        return self.get_session_digest(session_id)

    def get_session_digest(self, session_id: int) -> DigestResult:
        version = snapshot_session(session_id, settings=self.settings).source_version
        config = digest_config(self.settings)
        with connection(self.settings) as conn:
            row = conn.execute(
                "SELECT d.*,j.state AS job_state,j.error_code FROM digests d"
                " JOIN enrichment_jobs j ON j.id=d.job_id WHERE d.session_id=?"
                " ORDER BY (d.source_version=? AND d.config_id=?) DESC,d.id DESC LIMIT 1",
                (session_id, version, config),
            ).fetchone()
        if row is None:
            return DigestResult()
        stale = row["source_version"] != version or row["config_id"] != config
        markdown = ""
        if row["markdown_path"]:
            path = self.settings.data_dir / row["markdown_path"]
            try:
                markdown = path.read_text()
            except FileNotFoundError:
                return DigestResult(
                    id=row["id"],
                    state="failed",
                    job_id=row["job_id"],
                    error_code="artifact_missing",
                )
        state = {"running": "pending", "done": "ready"}.get(row["job_state"], row["job_state"])
        return DigestResult(
            id=row["id"],
            state="stale" if stale else state,
            source_version=row["source_version"],
            stale=stale,
            previous_output=bool(markdown) and state != "ready",
            job_id=row["job_id"],
            error_code=row["error_code"],
            markdown=markdown[:24000],
            markdown_path=row["markdown_path"],
            truncated=len(markdown) > 24000,
        )


def reconcile_digests(settings: Settings) -> int:
    if not settings.auto_digests or not settings.llm_configured:
        return 0
    service, added, after = DigestService(settings), 0, 0
    while True:
        with connection(settings) as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NOT NULL AND id>? ORDER BY id LIMIT 200",
                (after,),
            ).fetchall()
        if not rows:
            return added
        for row in rows:
            previous = service.get_session_digest(row["id"])
            result = service.request_digest(row["id"], origin="automatic")
            added += previous.job_id != result.job_id
        after = rows[-1]["id"]
