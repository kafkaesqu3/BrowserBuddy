import pytest

from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


def test_rrf_aggregates_and_fallback_stays_available(tmp_path):
    from browserbuddy.memory import MemoryService
    from browserbuddy.memory_models import SearchRequest
    from browserbuddy.search import fuse_hits

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "s", 1)
    storage.add_page_visit(
        session,
        url="https://t",
        title="quantum",
        text="quantum",
        visited_at=100,
        html_key=None,
        container=None,
    )
    service = MemoryService(settings)
    hits = service.search_memory(SearchRequest(query="quantum")).hits
    fused = fuse_hits(hits, hits)
    assert fused[0].score == pytest.approx(2 / 61)
    enabled = MemoryService(settings.model_copy(update={"semantic_enabled": True}))
    result = enabled.search_memory(SearchRequest(query="quantum"))
    assert result.mode == "fts"
    assert result.hits[0].source_id == hits[0].source_id
    assert "semantic_unavailable" in result.warnings
    conn.close()


def test_vector_search_filters_before_limit(tmp_path):
    from browserbuddy.embeddings import Chunk
    from browserbuddy.indexing import publish_index
    from browserbuddy.jobs import JobKey, JobStore
    from browserbuddy.memory import content_version
    from browserbuddy.memory_models import SearchRequest
    from browserbuddy.search import HybridSearch
    from browserbuddy.worker import embedding_identity

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "s", 1)
    jobs = JobStore(settings)
    wanted = None
    for at in range(202):
        url = f"https://test/{at}"
        page = storage.add_page_visit(
            session,
            url=url,
            title="Title",
            text="Evidence",
            visited_at=at,
            html_key=None,
            container=None,
        )
        jobs.enqueue(
            JobKey(
                "embed_page",
                page,
                content_version("Title", url, "Evidence"),
                embedding_identity(settings),
            )
        )
        job = jobs.claim(now=0)
        publish_index(
            job,
            settings=settings,
            chunks=[Chunk("Evidence", 0, "body", 0, 8)],
            vectors=[[1.0, 0.0]],
        )
        wanted = page

    class Encoder:
        def query(self, text):
            return [1.0, 0.0]

    search = HybridSearch(settings, Encoder())
    result = search.search(SearchRequest(query="synonym", since=201), source_kind="page")
    assert result.mode == "hybrid"
    assert [hit.source_id for hit in result.hits] == [wanted]
    assert result.hits[0].citation.start == 0
    conn.close()


def test_missing_native_extension_reports_lexical_fallback(tmp_path, monkeypatch):
    import json
    import sys

    from browserbuddy.memory import MemoryService
    from browserbuddy.memory_models import SearchRequest

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    conn = db.connect(settings.db_path)
    session = Storage(conn).start_session("c", "s", 1)
    Storage(conn).add_page_visit(
        session,
        url="https://t",
        title="quantum",
        text="qubits",
        visited_at=1,
        html_key=None,
        container=None,
    )
    (tmp_path / "embedding.json").write_text(
        json.dumps(
            {
                "model": settings.embedding_model,
                "requested_revision": "main",
                "revision": "A",
            }
        )
    )
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    result = MemoryService(settings).search_memory(SearchRequest(query="quantum"))
    assert result.mode == "fts"
    assert len(result.hits) == 1
    assert result.capabilities.semantic == "unavailable"
    conn.close()


def test_generation_change_during_query_returns_lexical_fallback(tmp_path):
    import json

    from browserbuddy.memory_models import SearchRequest
    from browserbuddy.search import HybridSearch

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    conn = db.connect(settings.db_path)
    session = Storage(conn).start_session("c", "s", 1)
    Storage(conn).add_page_visit(
        session,
        url="https://t",
        title="quantum",
        text="qubits",
        visited_at=1,
        html_key=None,
        container=None,
    )

    class Encoder:
        def query(self, text):
            (tmp_path / "embedding.json").write_text(
                json.dumps(
                    {
                        "model": settings.embedding_model,
                        "requested_revision": settings.embedding_revision,
                        "revision": "new-generation",
                    }
                )
            )
            return [1.0, 0.0]

    result = HybridSearch(settings, Encoder()).search(
        SearchRequest(query="quantum"), source_kind="page"
    )
    assert result.mode == "fts"
    assert len(result.hits) == 1
    assert "semantic_unavailable" in result.warnings
    conn.close()
