"""Operational endpoints registered only on an authenticated router."""

from typing import Annotated

from fastapi import HTTPException, Query

from browserbuddy.jobs import JobStore
from browserbuddy.memory_api import invoke
from browserbuddy.memory_models import (
    AskRequest,
    Contract,
    DigestResult,
    JobInfo,
    JobPage,
    RecallAnswer,
    SetupResult,
)


class DigestRequest(Contract):
    regenerate: bool = False


def register_operations(router, service) -> None:
    jobs = JobStore(service.settings)

    @router.post("/sessions/{session_id}/digest", status_code=202)
    def request_digest(session_id: int, request: DigestRequest) -> DigestResult:
        from browserbuddy.digests import DigestService

        return invoke(
            lambda: DigestService(service.settings).request_digest(
                session_id, regenerate=request.regenerate
            )
        )

    @router.post("/ask")
    async def ask(request: AskRequest) -> RecallAnswer:
        from browserbuddy.recall import RecallService

        try:
            return await RecallService(service.settings, service).ask(request)
        except ValueError as exc:
            raise HTTPException(422, "Invalid recall input or missing model configuration") from exc
        except Exception as exc:
            raise HTTPException(
                503, "Recall failed; check tool-calling support and model endpoint"
            ) from exc

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
