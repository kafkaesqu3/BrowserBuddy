def _page(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    pid = storage.add_page_visit(
        sid,
        url="https://a.com",
        title="A",
        visited_at=1100,
        text="",
        html_key=None,
        container=None,
    )
    return sid, pid


def test_add_page_dwell_accumulates_on_latest_visit(storage):
    sid, pid = _page(storage)
    assert storage.add_page_dwell(sid, url="https://a.com", dwell_ms=3000) is True
    assert storage.add_page_dwell(sid, url="https://a.com", dwell_ms=1500) is True
    row = storage._conn.execute("SELECT dwell_ms FROM page_visits WHERE id = ?", (pid,)).fetchone()
    assert row["dwell_ms"] == 4500


def test_add_page_dwell_targets_the_latest_visit_for_the_url(storage):
    sid, first = _page(storage)
    second = storage.add_page_visit(
        sid,
        url="https://a.com",
        title="A2",
        visited_at=1200,
        text="",
        html_key=None,
        container=None,
    )
    storage.add_page_dwell(sid, url="https://a.com", dwell_ms=999)
    rows = {
        r["id"]: r["dwell_ms"]
        for r in storage._conn.execute("SELECT id, dwell_ms FROM page_visits").fetchall()
    }
    assert rows[second] == 999
    assert rows[first] == 0


def test_add_page_dwell_orphan_returns_false(storage):
    sid, _ = _page(storage)
    assert storage.add_page_dwell(sid, url="https://never-seen.com", dwell_ms=100) is False
