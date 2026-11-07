import pytest
from pydantic import ValidationError

from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


@pytest.fixture
def memory(tmp_path):
    from browserbuddy.memory import MemoryService

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("test", "same name", 1)
    yield MemoryService(settings), storage, session
    conn.close()


def add_page(storage, session, *, at=100, text="quantum evidence", url="https://a.test"):
    return storage.add_page_visit(
        session,
        url=url,
        title="Quantum",
        visited_at=at,
        text=text,
        html_key=None,
        container=None,
    )


def test_time_filter_precedes_rank_limit(memory):
    from browserbuddy.memory_models import SearchRequest

    service, storage, session = memory
    for at in range(201):
        add_page(storage, session, at=at, text="quantum quantum quantum")
    wanted = add_page(storage, session, at=300)
    add_page(storage, session, at=400)
    result = service.search_memory(SearchRequest(query="quantum", since=300, until=400))
    assert [h.source_id for h in result.hits] == [wanted]
    assert result.hits[0].citation.source_id == wanted
    assert result.mode == "fts"


def test_page_slice_and_unknown_id(memory):
    service, storage, session = memory
    page_id = add_page(storage, session, text="0123456789αβγδε")
    page = service.get_page(page_id, offset=10, max_chars=3)
    assert page.text == "αβγ"
    assert page.next_offset == 13
    assert page.truncated
    assert service.get_page(page_id, offset=100).text == ""
    with pytest.raises(LookupError):
        service.get_page(999)


def test_sessions_pagination_and_snapshot_semantics(memory):
    service, storage, session = memory
    for at in range(2, 32):
        storage.start_session("test", "same name", at)
    first = service.list_sessions()
    second = service.list_sessions(after_id=first.next_cursor)
    assert len(first.items) == 20
    assert len(second.items) == 11
    assert second.items[0].id > first.items[-1].id
    assert first.items[0].id == session
    assert service.get_session_digest(session).state == "missing"


def test_literal_queries_and_distinct_recaptures(memory):
    from browserbuddy.memory_models import SearchRequest

    service, storage, session = memory
    a = add_page(storage, session, at=100)
    add_page(storage, session, at=200)
    b = add_page(storage, session, at=300, text="quantum different evidence")
    hits = service.search_memory(SearchRequest(query="quantum")).hits
    assert len(hits) == 2
    assert sum(h.collapsed_count for h in hits) == 3
    assert any(h.source_id == b for h in hits)
    assert service.search_memory(SearchRequest(query='" OR *')).hits == []
    assert service.search_memory(SearchRequest(query="  ")).hits == []
    assert service.get_page(a).session_id == session


def test_missing_screenshot_is_reported(memory):
    service, storage, session = memory
    page = add_page(storage, session)
    storage.add_page_screenshot(
        session,
        url="https://a.test",
        occurred_at=110,
        kind="periodic",
        blob_key="a" * 64,
        phash="0000000000000000",
        dedup_threshold=0,
    )
    screenshots = service.get_screenshots(page)
    assert len(screenshots.items) == 1
    assert screenshots.items[0].available is False


def test_invalid_query_bounds(memory):
    from browserbuddy.memory_models import SearchRequest

    service, _, session = memory
    for fields in ({"since": 20, "until": 20}, {"limit": 101}, {"session_id": 0}):
        with pytest.raises(ValidationError):
            SearchRequest(query="x", **fields)
    with pytest.raises(ValueError):
        service.search_history(SearchRequest(query="x", session_id=session))
