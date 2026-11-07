"""Typed authenticated client for CLI and MCP; never parses arbitrary error bodies."""

import httpx
from pydantic import TypeAdapter

from browserbuddy.config import Settings
from browserbuddy.memory_models import (
    Capabilities,
    DigestResult,
    JobInfo,
    JobPage,
    OpenTab,
    PageEvidence,
    RecallAnswer,
    ScreenshotPage,
    SearchRequest,
    SearchResponse,
    SessionPage,
    SetupResult,
)


class MemoryClient:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings, self.transport = settings, transport
        self.http = None

    async def __aenter__(self):
        try:
            secret = (self.settings.data_dir / "secret").read_text().strip()
        except OSError as exc:
            raise RuntimeError("Backend secret unavailable; run buddy serve first") from exc
        self.http = httpx.AsyncClient(
            base_url=f"http://{self.settings.host}:{self.settings.port}",
            headers={"X-BB-Secret": secret},
            timeout=100,
            transport=self.transport,
        )
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def request(
        self, method: str, path: str, response_type, *, payload=None, params=None, timeout=100
    ):
        """Send authenticated HTTP and validate its typed response.

        Args:
            method: HTTP method.
            path: Backend-relative endpoint path.
            response_type: Response validation contract.
            payload: JSON request body.
            params: Query parameters.
            timeout: HTTP timeout in seconds.
        """
        try:
            response = await self.http.request(
                method, path, json=payload, params=params, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                "Backend unavailable; run buddy serve and check its address"
            ) from exc
        if response.is_error:
            raise RuntimeError(f"Backend returned HTTP {response.status_code}; check configuration")
        return TypeAdapter(response_type).validate_python(response.json())

    async def search_memory(self, request: SearchRequest) -> SearchResponse:
        return await self.request(
            "POST", "/api/memory/search", SearchResponse, payload=request.model_dump()
        )

    async def search_history(self, request: SearchRequest) -> SearchResponse:
        return await self.request(
            "POST", "/api/history/search", SearchResponse, payload=request.model_dump()
        )

    async def get_open_tabs(self) -> list[OpenTab]:
        return await self.request("GET", "/api/tabs", list[OpenTab])

    async def list_sessions(self, *, after_id=0, limit=20) -> SessionPage:
        return await self.request(
            "GET", "/api/sessions", SessionPage, params={"after_id": after_id, "limit": limit}
        )

    async def get_page(self, page_visit_id: int, *, offset=0, max_chars=12000) -> PageEvidence:
        return await self.request(
            "GET",
            f"/api/pages/{page_visit_id}",
            PageEvidence,
            params={"offset": offset, "max_chars": max_chars},
        )

    async def get_screenshots(self, page_visit_id: int, *, after_id=0, limit=20) -> ScreenshotPage:
        return await self.request(
            "GET",
            f"/api/pages/{page_visit_id}/screenshots",
            ScreenshotPage,
            params={"after_id": after_id, "limit": limit},
        )

    async def get_session_digest(self, session_id: int) -> DigestResult:
        return await self.request("GET", f"/api/sessions/{session_id}/digest", DigestResult)

    async def capabilities(self) -> Capabilities:
        return await self.request("GET", "/api/capabilities", Capabilities)

    async def jobs(self, *, after_id=0, limit=20) -> JobPage:
        return await self.request(
            "GET", "/api/jobs", JobPage, params={"after_id": after_id, "limit": limit}
        )

    async def retry(self, job_id: int) -> JobInfo:
        return await self.request("POST", f"/api/jobs/{job_id}/retry", JobInfo)

    async def prepare(self) -> SetupResult:
        return await self.request("POST", "/api/index/prepare", SetupResult, timeout=1800)

    async def ask(self, request) -> RecallAnswer:
        return await self.request("POST", "/api/ask", RecallAnswer, payload=request.model_dump())

    async def digest(self, session_id: int, *, regenerate=False) -> DigestResult:
        return await self.request(
            "POST",
            f"/api/sessions/{session_id}/digest",
            DigestResult,
            payload={"regenerate": regenerate},
        )
