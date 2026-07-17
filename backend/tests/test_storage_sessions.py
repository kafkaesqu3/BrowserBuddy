import pytest


def test_start_end_and_active_session(storage):
    sid = storage.start_session("ext-1", "quantum research", started_at=1000)
    active = storage.get_active_session("ext-1")
    assert active["id"] == sid
    assert active["name"] == "quantum research"
    assert storage.get_active_session("other-client") is None
    assert storage.end_session("ext-1", ended_at=2000) is True
    assert storage.get_active_session("ext-1") is None
    assert storage.end_session("ext-1", ended_at=3000) is False  # idempotent


def test_starting_a_session_ends_the_previous_one(storage):
    first = storage.start_session("ext-1", "one", started_at=1000)
    second = storage.start_session("ext-1", "two", started_at=2000)
    assert storage.get_active_session("ext-1")["id"] == second
    row = storage._conn.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (first,)
    ).fetchone()
    assert row["ended_at"] == 2000  # implicitly closed at the new start


def test_add_page_visit_and_search(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    n = storage.add_page_visit(
        sid,
        url="https://arxiv.org/abs/42",
        title="Sparse retrieval methods",
        visited_at=1500,
        text="A survey of sparse retrieval and ranking methods.",
        html_key="abc123",
        container="Research",
    )
    assert n == 1
    # duplicate (session, url, visited_at) ignored
    assert storage.add_page_visit(
        sid, url="https://arxiv.org/abs/42", title="x", visited_at=1500, text="y",
        html_key=None, container=None,
    ) == 0
    results = storage.search_pages("sparse ranking")
    assert len(results) == 1
    assert results[0]["url"] == "https://arxiv.org/abs/42"
    assert results[0]["session_name"] == "s"


def test_add_page_visit_requires_valid_session(storage):
    with pytest.raises(LookupError):
        storage.add_page_visit(
            9999, url="https://a.com", title="", visited_at=1, text="",
            html_key=None, container=None,
        )


def test_list_sessions_with_page_counts(storage):
    sid = storage.start_session("ext-1", "s1", started_at=1000)
    storage.add_page_visit(sid, url="https://a.com", title="", visited_at=1100,
                           text="", html_key=None, container=None)
    storage.end_session("ext-1", ended_at=2000)
    storage.start_session("ext-1", "s2", started_at=3000)
    rows = storage.list_sessions()
    assert [r["name"] for r in rows] == ["s2", "s1"]  # newest first
    assert rows[1]["page_count"] == 1
    assert rows[0]["ended_at"] is None
