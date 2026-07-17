from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class ProtocolModel(BaseModel):
    # Forward-compat policy: an older backend must tolerate (and ignore) fields
    # added by a newer extension. Do NOT change to extra="forbid".
    model_config = ConfigDict(extra="ignore")


class HistoryVisit(ProtocolModel):
    url: str
    title: str = ""
    visit_time: int = Field(ge=0)  # ms since epoch, UTC


class TabRecord(ProtocolModel):
    tab_id: int
    window_id: int
    url: str
    title: str = ""
    # Optional enrichment; Firefox-only fields (container) stay null on other browsers.
    group_id: int | None = None
    container: str | None = None
    opened_at: int | None = Field(default=None, ge=0)
    last_activated_at: int | None = Field(default=None, ge=0)
    active_ms: int = Field(default=0, ge=0)


class TabStateEvent(ProtocolModel):
    type: Literal["tab_state"]
    tabs: list[TabRecord]


class HistoryVisitsEvent(ProtocolModel):
    type: Literal["history_visits"]
    visits: list[HistoryVisit]


class SessionStartEvent(ProtocolModel):
    type: Literal["session_start"]
    name: str = Field(min_length=1)
    started_at: int = Field(ge=0)


class SessionEndEvent(ProtocolModel):
    type: Literal["session_end"]
    ended_at: int = Field(ge=0)


class PageCaptureEvent(ProtocolModel):
    type: Literal["page_capture"]
    url: str = Field(min_length=1)
    title: str = ""
    visited_at: int = Field(ge=0)
    text: str = ""
    html: str | None = None  # raw DOM HTML; stored content-addressed, not in SQLite
    container: str | None = None


class AuthMessage(ProtocolModel):
    type: Literal["auth"]
    secret: str = Field(min_length=1)
    client_id: str = Field(min_length=1)


class EventEnvelope(ProtocolModel):
    """Versioned, sequence-numbered wrapper for every ingest event.

    An `error` frame from the server means the event was rejected — clients
    must skip or fix it, never blind-retry the same seq.
    """

    # TODO(protocol v2): reject v > PROTOCOL_VERSION in the WS handler
    v: int = Field(ge=1)
    seq: int = Field(ge=1)
    event: (
        TabStateEvent | HistoryVisitsEvent | SessionStartEvent | SessionEndEvent | PageCaptureEvent
    ) = Field(discriminator="type")
