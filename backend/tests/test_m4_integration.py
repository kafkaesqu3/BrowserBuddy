import asyncio
import json

import httpx
from fastmcp import Client
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from typer.testing import CliRunner

from browserbuddy import cli_memory
from browserbuddy.cli import app as cli
from browserbuddy.client import MemoryClient
from browserbuddy.digest_generation import generate_digest
from browserbuddy.jobs import JobStore
from browserbuddy.mcp import create_mcp
from browserbuddy.memory_models import SearchRequest
from browserbuddy.recall import RecallService


def test_capture_recall_mcp_and_digest_share_evidence(client, app, monkeypatch):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "secret": app.state.secret, "client_id": "m4"})
        assert ws.receive_json()["type"] == "auth_ok"
        events = [
            {"type": "session_start", "name": "Integration", "started_at": 100},
            {
                "type": "page_capture",
                "url": "https://test",
                "title": "Quantum",
                "text": "Quantum computers use qubits.",
                "visited_at": 110,
            },
            {"type": "session_start", "name": "Next", "started_at": 200},
        ]
        for seq, event in enumerate(events, 1):
            ws.send_json({"v": 1, "seq": seq, "event": event})
            assert ws.receive_json() == {"type": "ack", "seq": seq}
    headers = {"X-BB-Secret": app.state.secret}
    found = client.post(
        "/api/memory/search", headers=headers, json={"query": "quantum", "since": 100, "until": 200}
    ).json()
    page_id = found["hits"][0]["source_id"]
    session_id = found["hits"][0]["session_id"]

    def respond(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "text": "Quantum computers use qubits.",
                        "status": "answered",
                        "citations": [{"source_kind": "page", "source_id": page_id}],
                    },
                )
            ]
        )

    model = FunctionModel(respond)
    monkeypatch.setattr(RecallService, "provider_model", lambda _: model)
    answer = client.post(
        "/api/ask", headers=headers, json={"question": "quantum", "session_id": session_id}
    )
    assert answer.status_code == 200
    assert answer.json()["citations"][0]["source_id"] == page_id

    monkeypatch.setattr(
        cli_memory,
        "MemoryClient",
        lambda _: MemoryClient(app.state.settings, transport=httpx.ASGITransport(app=app)),
    )
    cli_answer = CliRunner().invoke(cli, ["ask", "quantum", "--session", str(session_id), "--json"])
    assert cli_answer.exit_code == 0, cli_answer.output
    assert json.loads(cli_answer.output)["citations"][0]["source_id"] == page_id

    async def mcp_read():
        async with (
            MemoryClient(app.state.settings, transport=httpx.ASGITransport(app=app)) as memory,
            Client(create_mcp(memory)) as mcp,
        ):
            result = await memory.search_memory(SearchRequest(query="qubits"))
            assert result.hits[0].source_id == page_id
            evidence = await mcp.call_tool("get_page", {"page_visit_id": page_id})
            assert "Quantum computers use qubits" in str(evidence)

    asyncio.run(mcp_read())
    response = client.post(
        f"/api/sessions/{session_id}/digest", headers=headers, json={"regenerate": False}
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    # Stop the real runner before deterministically injecting the model boundary.
    client.portal.call(app.state.worker.stop)
    jobs = JobStore(app.state.settings)
    job = jobs.claim(now=10**15)
    assert job.id == job_id
    asyncio.run(generate_digest(job, settings=app.state.settings, model=model))
    digest = client.get(f"/api/sessions/{session_id}/digest", headers=headers).json()
    assert digest["state"] == "ready"
    assert f"page:{page_id}" in digest["markdown"]
    assert jobs.get(job_id).state == "done"
