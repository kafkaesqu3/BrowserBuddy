import logging
import sqlite3

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret, verify_secret
from browserbuddy.config import Settings, get_settings
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    TabStateEvent,
)
from browserbuddy.storage import Storage

logger = logging.getLogger(__name__)

# Application-defined close code for failed auth (4000-4999 range).
WS_CLOSE_UNAUTHORIZED = 4401


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="BrowserBuddy")
    app.state.storage = Storage(db.connect(settings.db_path))
    app.state.secret = get_or_create_secret(settings.data_dir)

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
                        _apply_event(storage, envelope)
                        storage.set_last_seq(auth.client_id, envelope.seq)
                    except sqlite3.IntegrityError as exc:
                        logger.warning(
                            "Integrity error applying event seq=%d: %s", envelope.seq, exc
                        )
                        await ws.send_json({"type": "error", "detail": "integrity error"})
                        continue
                await ws.send_json({"type": "ack", "seq": envelope.seq})
        except WebSocketDisconnect:
            pass

    return app


def _apply_event(storage: Storage, envelope: EventEnvelope) -> None:
    event = envelope.event
    if isinstance(event, TabStateEvent):
        storage.replace_open_tabs(event.tabs)
    elif isinstance(event, HistoryVisitsEvent):
        storage.insert_history_visits(event.visits, source="sync")
    else:  # pragma: no cover - unreachable until the union grows
        raise AssertionError(f"unhandled event type: {type(event).__name__}")
