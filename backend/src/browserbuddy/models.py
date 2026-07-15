from typing import Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


class HistoryVisit(BaseModel):
    url: str
    title: str = ""
    visit_time: int  # ms since epoch, UTC


class TabRecord(BaseModel):
    tab_id: int
    window_id: int
    url: str
    title: str = ""
    # Optional enrichment; Firefox-only fields (container) stay null on other browsers.
    group_id: int | None = None
    container: str | None = None
    opened_at: int | None = None
    last_activated_at: int | None = None
    active_ms: int = 0


class TabStateEvent(BaseModel):
    type: Literal["tab_state"]
    tabs: list[TabRecord]


class HistoryVisitsEvent(BaseModel):
    type: Literal["history_visits"]
    visits: list[HistoryVisit]


class AuthMessage(BaseModel):
    type: Literal["auth"]
    secret: str
    client_id: str


class EventEnvelope(BaseModel):
    """Versioned, sequence-numbered wrapper for every ingest event."""

    v: int = Field(ge=1)
    seq: int = Field(ge=1)
    event: TabStateEvent | HistoryVisitsEvent = Field(discriminator="type")
