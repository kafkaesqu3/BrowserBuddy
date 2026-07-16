import pytest
from starlette.websockets import WebSocketDisconnect


def _auth(ws, app, client_id="ext-test"):
    ws.send_json({"type": "auth", "secret": app.state.secret, "client_id": client_id})
    return ws.receive_json()


def _tab_state_envelope(seq, urls):
    return {
        "v": 1,
        "seq": seq,
        "event": {
            "type": "tab_state",
            "tabs": [{"tab_id": i, "window_id": 1, "url": u} for i, u in enumerate(urls)],
        },
    }


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "protocol": 1}


def test_ws_rejects_bad_secret(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "secret": "wrong", "client_id": "x"})
        ws.receive_json()


def test_ws_ingests_tab_state(client, app):
    with client.websocket_connect("/ws") as ws:
        assert _auth(ws, app) == {"type": "auth_ok", "last_seq": 0}
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    tabs = app.state.storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://a.com"]


def test_ws_replayed_seq_is_acked_but_not_applied(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        ws.receive_json()
        # Same seq, different content: must be acked (so the client advances)
        # but NOT applied (idempotency).
        ws.send_json(_tab_state_envelope(1, ["https://EVIL.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    assert [t["url"] for t in app.state.storage.list_open_tabs()] == ["https://a.com"]


def test_ws_resume_reports_last_seq(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        ws.receive_json()
    with client.websocket_connect("/ws") as ws:
        assert _auth(ws, app) == {"type": "auth_ok", "last_seq": 1}


def test_ws_malformed_event_gets_error_not_disconnect(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json({"v": 1, "seq": 1, "event": {"type": "bogus"}})
        assert ws.receive_json()["type"] == "error"
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_history_visits_ingested(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            {
                "v": 1,
                "seq": 1,
                "event": {
                    "type": "history_visits",
                    "visits": [
                        {
                            "url": "https://arxiv.org/abs/2",
                            "title": "Retrieval survey",
                            "visit_time": 1720000000000,
                        }
                    ],
                },
            }
        )
        assert ws.receive_json() == {"type": "ack", "seq": 1}
    results = app.state.storage.search_history("retrieval")
    assert len(results) == 1


def test_ws_non_json_after_auth_closes_1003(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 1003


def test_ws_binary_first_frame_rejected(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_ws_preauth_disconnect_is_clean(client, app):
    # Client connects and drops without ever sending auth; server must survive.
    with client.websocket_connect("/ws"):
        pass
    with client.websocket_connect("/ws") as ws:
        assert _auth(ws, app) == {"type": "auth_ok", "last_seq": 0}
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_integrity_error_gets_error_frame_and_connection_survives(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        dup = {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "tab_state",
                "tabs": [
                    {"tab_id": 1, "window_id": 1, "url": "https://a.com"},
                    {"tab_id": 1, "window_id": 1, "url": "https://b.com"},
                ],
            },
        }
        ws.send_json(dup)
        assert ws.receive_json()["type"] == "error"
        ws.send_json(_tab_state_envelope(2, ["https://c.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 2}
    assert [t["url"] for t in app.state.storage.list_open_tabs()] == ["https://c.com"]


def test_ws_ping_gets_pong(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json(_tab_state_envelope(1, ["https://a.com"]))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_stale_connection_cannot_apply_old_snapshot(client, app):
    # Two live sockets for the same client_id: the stale one must not
    # overwrite newer state, even though its connection-local view is old.
    with client.websocket_connect("/ws") as stale:
        _auth(stale, app, client_id="ext-1")
        with client.websocket_connect("/ws") as fresh:
            _auth(fresh, app, client_id="ext-1")
            fresh.send_json(_tab_state_envelope(2, ["https://new.com"]))
            assert fresh.receive_json() == {"type": "ack", "seq": 2}
        # Stale socket now replays seq 1: acked (so its client advances)
        # but NOT applied.
        stale.send_json(_tab_state_envelope(1, ["https://old.com"]))
        assert stale.receive_json() == {"type": "ack", "seq": 1}
    tabs = app.state.storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://new.com"]
