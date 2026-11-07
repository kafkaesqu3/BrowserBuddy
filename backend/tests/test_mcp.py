import asyncio

import httpx

from browserbuddy.config import Settings


def test_mcp_lists_only_read_tools_and_returns_typed_evidence(tmp_path):
    from fastmcp import Client

    from browserbuddy.client import MemoryClient
    from browserbuddy.mcp import create_mcp

    (tmp_path / "secret").write_text("secret")

    def handle(request):
        assert request.headers["X-BB-Secret"] == "secret"
        return httpx.Response(200, json={"hits": [], "warnings": ["semantic_unavailable"]})

    async def run():
        async with (
            MemoryClient(
                Settings(data_dir=tmp_path), transport=httpx.MockTransport(handle)
            ) as memory,
            Client(create_mcp(memory)) as client,
        ):
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {
                "search_memory",
                "search_history",
                "get_page",
                "get_open_tabs",
                "list_sessions",
                "get_screenshots",
                "get_session_digest",
            }
            result = await client.call_tool("search_memory", {"request": {"query": "x"}})
            assert not result.is_error
            assert "semantic_unavailable" in str(result)

    asyncio.run(run())
