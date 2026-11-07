"""Validated contracts shared by retrieval, HTTP, CLI, and agent tools."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchRequest(Contract):
    query: str = Field(max_length=2000)
    since: int | None = Field(default=None, ge=0)
    until: int | None = Field(default=None, ge=0)
    session_id: int | None = Field(default=None, ge=1)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def trim_query(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("since must precede until")
        return self


class Citation(Contract):
    source_kind: Literal["page", "history"]
    source_id: int = Field(ge=1)
    chunk_id: int | None = None
    start: int | None = None
    end: int | None = None

    @property
    def reference(self) -> str:
        return f"{self.source_kind}:{self.source_id}"


class SearchHit(Contract):
    source_kind: Literal["page", "history"]
    source_id: int
    session_id: int | None = None
    session_name: str | None = None
    url: str
    title: str
    captured_at: int
    excerpt: str
    citation: Citation
    chunk_id: int | None = None
    score: float
    collapsed_count: int = 1
    content_hash: str


class Capabilities(Contract):
    semantic: Literal["disabled", "unavailable", "indexing", "ready"] = "disabled"
    indexed: int = 0
    eligible: int = 0
    llm_configured: bool = False
    ocr: bool = False
    screenshots: Literal["metadata-only"] = "metadata-only"


class SearchResponse(Contract):
    hits: list[SearchHit] = Field(default_factory=list)
    truncated: bool = False
    mode: Literal["fts", "hybrid"] = "fts"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    warnings: list[str] = Field(default_factory=list)


class PageEvidence(Contract):
    id: int
    session_id: int
    session_name: str
    url: str
    title: str
    visited_at: int
    text: str
    offset: int
    next_offset: int | None
    truncated: bool
    dwell_ms: int
    dwell_approximate: bool = True
    html_available: bool


class SessionInfo(Contract):
    id: int
    name: str
    started_at: int
    ended_at: int | None
    page_count: int


class OpenTab(Contract):
    tab_id: int
    window_id: int
    url: str
    title: str
    group_id: int | None
    container: str | None
    opened_at: int | None
    last_activated_at: int | None
    active_ms: int
    snapshot: bool = True


class ScreenshotInfo(Contract):
    id: int
    page_visit_id: int
    captured_at: int
    kind: str
    blob_key: str
    available: bool


class ResultPage[T](Contract):
    items: list[T]
    next_cursor: int | None = None
    truncated: bool = False


SessionPage = ResultPage[SessionInfo]
ScreenshotPage = ResultPage[ScreenshotInfo]


class JobInfo(Contract):
    id: int
    kind: str
    source_id: int
    source_version: str
    config_id: str
    origin: Literal["automatic", "explicit"]
    state: Literal["pending", "running", "done", "failed"]
    attempt: int = 0
    next_attempt_at: int = 0
    claim_token: str | None = None
    error_code: str | None = None


JobPage = ResultPage[JobInfo]


class DigestResult(Contract):
    id: int | None = None
    state: Literal["missing", "pending", "failed", "ready", "stale"] = "missing"
    source_version: str | None = None
    stale: bool = False
    previous_output: bool = False
    job_id: int | None = None
    error_code: str | None = None
    markdown: str = ""
    markdown_path: str | None = None
    truncated: bool = False


class SetupResult(Contract):
    config_id: str | None = None
    ready: bool
    error_code: str | None = None


class AskRequest(SearchRequest):
    question: str = Field(min_length=1, max_length=2000)
    query: str = ""


class RecallAnswer(Contract):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    since: int | None = None
    until: int | None = None
    warnings: list[str] = Field(default_factory=list)
    status: Literal["answered", "insufficient_evidence"]
