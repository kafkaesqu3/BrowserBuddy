import logging
import sqlite3

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret, verify_secret
from browserbuddy.blobs import BlobStore
from browserbuddy.config import Settings, get_settings
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    LinkClickEvent,
    NavEdgeEvent,
    PageCaptureEvent,
    SessionEndEvent,
    SessionStartEvent,
    TabStateEvent,
)
from browserbuddy.storage import NoActiveSessionError, Storage

logger = logging.getLogger(__name__)

# Application-defined close code for failed auth (4000-4999 range).
WS_CLOSE_UNAUTHORIZED = 4401


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="BrowserBuddy")
    app.state.storage = Storage(db.connect(settings.db_path))
    app.state.secret = get_or_create_secret(settings.data_dir)
    app.state.blobs = BlobStore(settings.data_dir / "blobs")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "protocol": PROTOCOL_VERSION}

    @app.websocket("/ws")
    async def ws_ingest(ws: WebSocket) -> None:
        storage: Storage = app.state.storage
        # No Origin check by design: the shared secret, not the extension
        # origin, is the auth (see design spec, portability policy).
        await ws.accept()
        try:
            auth = AuthMessage.model_validate(await ws.receive_json())
        except WebSocketDisconnect:
            return
        except (ValidationError, ValueError, KeyError):
            # KeyError: starlette's receive_json on a binary frame.
            # JSONDecodeError is a ValueError subclass, already covered.
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        if not verify_secret(app.state.secret, auth.secret):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return

        last_seq = storage.get_last_seq(auth.client_id)
        await ws.send_json({"type": "auth_ok", "last_seq": last_seq})

        try:
            while True:
                try:
                    raw = await ws.receive_json()
                except (ValueError, KeyError):
                    # Not JSON / binary frame: unsupported data, close per RFC 6455.
                    await ws.close(code=1003)
                    return
                # Extension heartbeat (keeps MV3 worker alive). Match on the
                # type field only: newer clients may add fields to ping frames.
                if isinstance(raw, dict) and raw.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                    continue
                try:
                    envelope = EventEnvelope.model_validate(raw)
                except ValidationError as exc:
                    logger.warning("Rejected malformed event: %s", exc)
                    await ws.send_json({"type": "error", "detail": exc.errors()[0]["msg"]})
                    continue
                # Re-read the cursor per event: a concurrent connection for the
                # same client_id may have advanced it (stale-socket protection).
                last_seq = storage.get_last_seq(auth.client_id)
                if envelope.seq > last_seq:
                    try:
                        _apply_event(storage, app.state.blobs, auth.client_id, envelope)
                        storage.set_last_seq(auth.client_id, envelope.seq)
                    except (sqlite3.IntegrityError, NoActiveSessionError) as exc:
                        logger.warning("Rejected event seq=%d: %s", envelope.seq, exc)
                        await ws.send_json({"type": "error", "detail": str(exc)})
                        continue
                await ws.send_json({"type": "ack", "seq": envelope.seq})
        except WebSocketDisconnect:
            pass

    return app


def _apply_event(
    storage: Storage, blobs: BlobStore, client_id: str, envelope: EventEnvelope
) -> None:
    event = envelope.event
    if isinstance(event, TabStateEvent):
        storage.replace_open_tabs(event.tabs)
    elif isinstance(event, HistoryVisitsEvent):
        storage.insert_history_visits(event.visits, source="sync")
    elif isinstance(event, SessionStartEvent):
        storage.start_session(client_id, event.name, event.started_at)
    elif isinstance(event, SessionEndEvent):
        storage.end_session(client_id, event.ended_at)  # False = already ended; fine
    elif isinstance(event, PageCaptureEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_capture with no active session")
        html_key = blobs.put(event.html.encode()) if event.html else None
        page_visit_id = storage.add_page_visit(
            active["id"],
            url=event.url,
            title=event.title,
            visited_at=event.visited_at,
            text=event.text,
            html_key=html_key,
            container=event.container,
        )
        if event.links:
            storage.add_outbound_links(page_visit_id, event.links)
    elif isinstance(event, NavEdgeEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("nav_edge with no active session")
        storage.add_nav_edge(
            active["id"],
            from_url=event.from_url,
            to_url=event.to_url,
            transition=event.transition,
            qualifiers=event.qualifiers,
            query=event.query,
            occurred_at=event.occurred_at,
        )
    elif isinstance(event, LinkClickEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("link_click with no active session")
        storage.mark_link_clicked(
            active["id"], page_url=event.page_url, href=event.href, text=event.text
        )
    else:  # pragma: no cover - unreachable until the union grows
        raise AssertionError(f"unhandled event type: {type(event).__name__}")
