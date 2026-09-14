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


def test_ws_ping_with_extra_fields_still_gets_pong(client, app):
    """Forward-compat: newer clients may add fields to ping frames."""
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json({"type": "ping", "t": 123})
        assert ws.receive_json() == {"type": "pong"}


def _envelope(seq, event):
    return {"v": 1, "seq": seq, "event": event}


def test_ws_session_lifecycle_and_page_capture(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(1, {"type": "session_start", "name": "deep dive", "started_at": 100})
        )
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(
                2,
                {
                    "type": "page_capture",
                    "url": "https://arxiv.org/abs/7",
                    "title": "Grounded decoding",
                    "visited_at": 150,
                    "text": "grounded decoding methods for agents",
                    "html": "<html><body>grounded decoding</body></html>",
                    "container": "Research",
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(_envelope(3, {"type": "session_end", "ended_at": 200}))
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    storage = app.state.storage
    sessions = storage.list_sessions()
    assert sessions[0]["name"] == "deep dive"
    assert sessions[0]["page_count"] == 1
    results = storage.search_pages("grounded")
    assert results[0]["session_name"] == "deep dive"
    # html stored content-addressed
    row = storage._conn.execute("SELECT html_key FROM page_visits").fetchone()
    assert row["html_key"] is not None
    assert b"grounded decoding" in app.state.blobs.get(row["html_key"])


def test_ws_page_capture_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(1, {"type": "page_capture", "url": "https://a.com", "visited_at": 1})
        )
        reply = ws.receive_json()
        assert reply["type"] == "error"
        # connection survives; a session_start still works
        ws.send_json(_envelope(2, {"type": "session_start", "name": "s", "started_at": 2}))
        assert ws.receive_json() == {"type": "ack", "seq": 2}


def test_ws_session_end_without_session_is_idempotent_ack(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_end", "ended_at": 5}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}


def test_ws_page_capture_without_html_stores_no_blob(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 1}))
        ws.receive_json()
        ws.send_json(
            _envelope(
                2,
                {
                    "type": "page_capture",
                    "url": "https://a.com",
                    "visited_at": 2,
                    "text": "plain text only",
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
    row = app.state.storage._conn.execute("SELECT html_key FROM page_visits").fetchone()
    assert row["html_key"] is None


def test_ws_duplicate_page_capture_is_acked_not_errored(client, app):
    capture = {"type": "page_capture", "url": "https://a.com", "visited_at": 2, "text": "t"}
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 1}))
        ws.receive_json()
        ws.send_json(_envelope(2, capture))
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(_envelope(3, capture))  # same page again: absorbed, not error
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    assert app.state.storage._conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0] == 1


def test_ws_stale_connection_cannot_apply_old_snapshot(client, app):
    # With two connections sharing a client_id, the per-event DB read (not a
    # connection-local cache) prevents the stale socket from regressing state.
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


def test_ws_page_capture_stores_outbound_links(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(
                2,
                {
                    "type": "page_capture",
                    "url": "https://a.com",
                    "visited_at": 150,
                    "text": "body",
                    "links": [{"href": "https://x.com", "text": "X"}],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
    storage = app.state.storage
    sid = storage.list_sessions()[0]["id"]
    assert {r["href"] for r in storage.list_unfollowed_links(sid)} == {"https://x.com"}


def test_ws_nav_edge_and_link_click(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(
                2,
                {
                    "type": "page_capture",
                    "url": "https://a.com",
                    "visited_at": 150,
                    "text": "b",
                    "links": [{"href": "https://x.com", "text": "X"}],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "nav_edge",
                    "from_url": "https://a.com",
                    "to_url": "https://x.com",
                    "transition": "link",
                    "occurred_at": 160,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
        ws.send_json(
            _envelope(
                4,
                {
                    "type": "link_click",
                    "page_url": "https://a.com",
                    "href": "https://x.com",
                    "text": "X",
                    "clicked_at": 165,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 4}
    storage = app.state.storage
    sid = storage.list_sessions()[0]["id"]
    edges = storage.list_nav_edges(sid)
    assert edges[0]["to_url"] == "https://x.com"
    assert all(r["href"] != "https://x.com" for r in storage.list_unfollowed_links(sid))


def test_ws_nav_edge_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(1, {"type": "nav_edge", "to_url": "https://a.com", "occurred_at": 1})
        )
        assert ws.receive_json()["type"] == "error"


def test_ws_link_click_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                1,
                {
                    "type": "link_click",
                    "page_url": "https://a.com",
                    "href": "https://x.com",
                    "clicked_at": 1,
                },
            )
        )
        assert ws.receive_json()["type"] == "error"


def test_ws_page_dwell_accumulates_on_page_visit(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        assert ws.receive_json() == {"type": "ack", "seq": 1}
        ws.send_json(
            _envelope(
                2,
                {"type": "page_capture", "url": "https://a.com", "visited_at": 150, "text": "b"},
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 2}
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_dwell",
                    "url": "https://a.com",
                    "dwell_ms": 4000,
                    "occurred_at": 160,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
        ws.send_json(
            _envelope(
                4,
                {
                    "type": "page_dwell",
                    "url": "https://a.com",
                    "dwell_ms": 1500,
                    "occurred_at": 170,
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 4}
    row = app.state.storage._conn.execute("SELECT dwell_ms FROM page_visits").fetchone()
    assert row["dwell_ms"] == 5500  # additive across two flushes


def test_ws_page_dwell_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                1, {"type": "page_dwell", "url": "https://a.com", "dwell_ms": 1, "occurred_at": 1}
            )
        )
        assert ws.receive_json()["type"] == "error"


def test_upload_blob_returns_content_key(client, app):
    resp = client.post("/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"hello")
    assert resp.status_code == 200
    import hashlib

    assert resp.json() == {"key": hashlib.sha256(b"hello").hexdigest()}


def test_upload_blob_rejects_bad_secret(client):
    resp = client.post("/api/blobs", headers={"X-BB-Secret": "wrong"}, content=b"hello")
    assert resp.status_code == 401


def test_upload_blob_rejects_missing_secret(client):
    resp = client.post("/api/blobs", content=b"hello")
    assert resp.status_code == 401


def test_upload_blob_rejects_oversized_body(client, app):
    resp = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"x" * (10_000_001)
    )
    assert resp.status_code == 413


def test_upload_blob_is_idempotent_and_uncompressed(client, app, tmp_path):
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"same bytes"
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"same bytes"
    ).json()["key"]
    assert key1 == key2
    assert app.state.blobs.get(key1) == b"same bytes"


def _jpeg(color=(10, 20, 30), size=(16, 16), quality=95):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _gradient_jpeg(ascending, width=32, height=32):
    import io

    from PIL import Image

    img = Image.new("L", (width, height))
    for x in range(width):
        value = int(255 * x / (width - 1))
        for y in range(height):
            img.putpixel((x, y), value if ascending else 255 - value)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _start_session_and_capture(client, app, url="https://a.com"):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        ws.receive_json()
        ws.send_json(
            _envelope(2, {"type": "page_capture", "url": url, "visited_at": 150, "text": "b"})
        )
        ws.receive_json()


def test_ws_page_screenshot_viewport_stores_row_and_reuses_key(client, app):
    _start_session_and_capture(client, app)
    key = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=_jpeg()
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 200,
                    "kind": "periodic",
                    "blob_keys": [key],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "periodic"
    assert rows[0]["blob_key"] == key  # single tile: no re-upload, key reused


def test_ws_page_screenshot_fullpage_stitches_tiles_into_a_new_blob(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((10, 10, 10), (20, 10))
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((20, 20, 20), (20, 15))
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 200,
                    "kind": "on_demand_fullpage",
                    "blob_keys": [key1, key2],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1
    assert rows[0]["blob_key"] not in (key1, key2)  # a new stitched blob was created
    import io

    from PIL import Image

    stitched = app.state.blobs.get(rows[0]["blob_key"])
    assert Image.open(io.BytesIO(stitched)).size == (20, 25)


def test_ws_page_screenshot_dedupes_visually_identical_capture(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    # Two solid-color images always difference-hash to the same value
    # (every adjacent-pixel comparison is equal->False), regardless of the
    # exact color/quality — different bytes/keys, same perceptual hash.
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((10, 20, 30), quality=95)
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((11, 21, 29), quality=80)
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        for seq, key in ((3, key1), (4, key2)):
            ws.send_json(
                _envelope(
                    seq,
                    {
                        "type": "page_screenshot",
                        "url": "https://a.com",
                        "occurred_at": 200 + seq,
                        "kind": "periodic",
                        "blob_keys": [key],
                    },
                )
            )
            # ack either way; dedup isn't an error
            assert ws.receive_json() == {"type": "ack", "seq": seq}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1  # second capture deduped away


def test_ws_page_screenshot_keeps_visually_distinct_captures(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_gradient_jpeg(True)
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_gradient_jpeg(False)
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        for seq, key in ((3, key1), (4, key2)):
            ws.send_json(
                _envelope(
                    seq,
                    {
                        "type": "page_screenshot",
                        "url": "https://a.com",
                        "occurred_at": 200 + seq,
                        "kind": "periodic",
                        "blob_keys": [key],
                    },
                )
            )
            assert ws.receive_json() == {"type": "ack", "seq": seq}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 2


def test_ws_page_screenshot_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                1,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 1,
                    "kind": "periodic",
                    "blob_keys": ["deadbeef"],
                },
            )
        )
        assert ws.receive_json()["type"] == "error"
