"""Authenticated HTTP adapters for memory services."""

import sqlite3
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from browserbuddy.auth import get_or_create_secret, verify_secret
from browserbuddy.memory import MemoryService
from browserbuddy.memory_models import (
    Capabilities,
    DigestResult,
    OpenTab,
    PageEvidence,
    ScreenshotPage,
    SearchRequest,
    SearchResponse,
    SessionPage,
)


def invoke[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(503, detail="Database unavailable; retry shortly") from exc


def create_memory_router(service: MemoryService) -> APIRouter:
    """Create shared-secret-protected evidence routes.

    Args:
        service: Retrieval service using operation-owned database connections.
    """
    secret = get_or_create_secret(service.settings.data_dir)

    def authorize(x_bb_secret: Annotated[str, Header()] = "") -> None:
        if not verify_secret(secret, x_bb_secret):
            raise HTTPException(401, detail="bad secret")

    router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @router.post("/memory/search")
    def search_memory(request: SearchRequest) -> SearchResponse:
        return invoke(lambda: service.search_memory(request))

    @router.post("/history/search")
    def search_history(request: SearchRequest) -> SearchResponse:
        return invoke(lambda: service.search_history(request))

    @router.get("/capabilities")
    def capabilities() -> Capabilities:
        return service.capabilities()

    @router.get("/tabs")
    def tabs() -> list[OpenTab]:
        return invoke(service.get_open_tabs)

    @router.get("/sessions")
    def sessions(
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SessionPage:
        return invoke(lambda: service.list_sessions(after_id=after_id, limit=limit))

    @router.get("/pages/{page_id}")
    def page(
        page_id: int,
        offset: Annotated[int, Query(ge=0)] = 0,
        max_chars: Annotated[int, Query(ge=1, le=24000)] = 12000,
    ) -> PageEvidence:
        return invoke(lambda: service.get_page(page_id, offset=offset, max_chars=max_chars))

    @router.get("/pages/{page_id}/screenshots")
    def screenshots(
        page_id: int,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ScreenshotPage:
        return invoke(lambda: service.get_screenshots(page_id, after_id=after_id, limit=limit))

    @router.get("/sessions/{session_id}/digest")
    def digest(session_id: int) -> DigestResult:
        return invoke(lambda: service.get_session_digest(session_id))

    from browserbuddy.operations_api import register_operations

    register_operations(router, service)
    return router
