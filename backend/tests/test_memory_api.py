import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/api/tabs",
        "/api/sessions",
        "/api/pages/1",
        "/api/pages/1/screenshots",
        "/api/sessions/1/digest",
        "/api/capabilities",
    ],
)
def test_retrieval_requires_secret(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-BB-Secret": "wrong"}).status_code == 401


def test_search_validation_and_results(client, app):
    headers = {"X-BB-Secret": app.state.secret}
    path = "/api/memory/search"
    assert client.post(path, json={"query": "x"}).status_code == 401
    result = client.post(path, json={"query": "x"}, headers=headers)
    assert result.status_code == 200
    assert result.json()["hits"] == []
    assert (
        client.post(
            path, json={"query": "x", "since": 20, "until": 10}, headers=headers
        ).status_code
        == 422
    )
    assert client.get("/api/pages/1", headers=headers).status_code == 404
    assert client.get("/api/sessions?limit=101", headers=headers).status_code == 422
    assert (
        client.post(
            "/api/history/search", json={"query": "x", "session_id": 1}, headers=headers
        ).status_code
        == 422
    )


def test_retrieval_resolves_stored_evidence(client, app):
    storage = app.state.storage
    session = storage.start_session("test", "Research", 1)
    page = storage.add_page_visit(
        session,
        url="https://test",
        title="Quantum",
        text="Stored evidence",
        visited_at=2,
        html_key=None,
        container=None,
    )
    headers = {"X-BB-Secret": app.state.secret}
    result = client.get(f"/api/pages/{page}?offset=7&max_chars=8", headers=headers)
    assert result.json()["text"] == "evidence"
    result = client.post("/api/memory/search", json={"query": "Stored"}, headers=headers)
    assert result.json()["hits"][0]["citation"]["source_id"] == page
