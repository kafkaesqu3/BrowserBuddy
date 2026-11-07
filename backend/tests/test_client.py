import asyncio

import httpx
import pytest

from browserbuddy.config import Settings


def test_client_auth_and_typed_results(tmp_path):
    from browserbuddy.client import MemoryClient
    from browserbuddy.memory_models import SearchRequest

    (tmp_path / "secret").write_text("test-secret")

    def handle(request):
        if request.headers.get("X-BB-Secret") != "test-secret":
            return httpx.Response(401)
        return httpx.Response(200, json={"hits": [], "mode": "fts"})

    async def run():
        async with MemoryClient(
            Settings(data_dir=tmp_path), transport=httpx.MockTransport(handle)
        ) as client:
            response = await client.search_memory(SearchRequest(query="test"))
            assert response.hits == []
            assert response.mode == "fts"

    asyncio.run(run())


def test_client_redacts_provider_response(tmp_path):
    from browserbuddy.client import MemoryClient
    from browserbuddy.memory_models import SearchRequest

    (tmp_path / "secret").write_text("sensitive-secret")

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(500, text="sensitive-secret"))
        async with MemoryClient(Settings(data_dir=tmp_path), transport=transport) as client:
            with pytest.raises(RuntimeError) as error:
                await client.search_memory(SearchRequest(query="test"))
            assert "sensitive-secret" not in str(error.value)

    asyncio.run(run())
