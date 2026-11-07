"""Operational endpoints registered only on an authenticated router."""

from typing import Annotated

from fastapi import HTTPException, Query

from browserbuddy.jobs import JobStore
from browserbuddy.memory_api import invoke
from browserbuddy.memory_models import JobInfo, JobPage, SetupResult


def register_operations(router, service) -> None:
    jobs = JobStore(service.settings)

    @router.get("/jobs")
    def list_jobs(
        after_id: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> JobPage:
        return invoke(lambda: jobs.list(after_id=after_id, limit=limit))

    @router.post("/jobs/{job_id}/retry")
    def retry(job_id: int) -> JobInfo:
        try:
            return invoke(lambda: jobs.retry(job_id))
        except HTTPException as exc:
            if exc.status_code == 422:
                raise HTTPException(409, "only failed jobs may be retried") from exc
            raise

    @router.post("/index/prepare")
    def prepare() -> SetupResult:
        try:
            return service.hybrid.embedder.prepare()
        except Exception as exc:
            raise HTTPException(
                503,
                "Model setup failed; install the semantic extra and check "
                "network access/model configuration",
            ) from exc
