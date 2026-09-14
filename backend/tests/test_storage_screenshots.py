def _page(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    pid = storage.add_page_visit(
        sid, url="https://a.com", title="A", visited_at=1100, text="", html_key=None, container=None
    )
    return sid, pid


def test_add_page_screenshot_inserts_and_returns_id(storage):
    sid, pid = _page(storage)
    new_id = storage.add_page_screenshot(
        sid,
        url="https://a.com",
        occurred_at=1200,
        kind="periodic",
        blob_key="key1",
        phash="0000000000000000",
        dedup_threshold=8,
    )
    assert isinstance(new_id, int)
    row = storage._conn.execute(
        "SELECT page_visit_id, blob_key, kind, phash FROM screenshots WHERE id = ?", (new_id,)
    ).fetchone()
    assert row["page_visit_id"] == pid
    assert row["blob_key"] == "key1"
    assert row["kind"] == "periodic"


def test_add_page_screenshot_dedupes_near_identical_capture(storage):
    sid, _ = _page(storage)
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="key1", phash="0000000000000000", dedup_threshold=8,
    )
    second = storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1300, kind="periodic",
        blob_key="key2", phash="0000000000000003", dedup_threshold=8,  # distance 2, <= 8
    )
    assert second is None
    count = storage._conn.execute("SELECT COUNT(*) AS c FROM screenshots").fetchone()["c"]
    assert count == 1


def test_add_page_screenshot_keeps_capture_beyond_threshold(storage):
    sid, _ = _page(storage)
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="key1", phash="0000000000000000", dedup_threshold=8,
    )
    second = storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1300, kind="periodic",
        blob_key="key2", phash="ffffffffffffffff", dedup_threshold=8,  # distance 64
    )
    assert isinstance(second, int)
    count = storage._conn.execute("SELECT COUNT(*) AS c FROM screenshots").fetchone()["c"]
    assert count == 2


def test_add_page_screenshot_orphan_returns_none(storage):
    sid, _ = _page(storage)
    result = storage.add_page_screenshot(
        sid, url="https://never-seen.com", occurred_at=1, kind="periodic",
        blob_key="k", phash="0" * 16, dedup_threshold=8,
    )
    assert result is None


def test_list_screenshots_filters_by_url(storage):
    sid, _ = _page(storage)
    storage.add_page_visit(
        sid, url="https://b.com", title="B", visited_at=1150, text="", html_key=None, container=None
    )
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="ka", phash="0" * 16, dedup_threshold=8,
    )
    storage.add_page_screenshot(
        sid, url="https://b.com", occurred_at=1250, kind="periodic",
        blob_key="kb", phash="f" * 16, dedup_threshold=8,
    )
    all_rows = storage.list_screenshots(sid)
    assert len(all_rows) == 2
    a_rows = storage.list_screenshots(sid, url="https://a.com")
    assert [r["blob_key"] for r in a_rows] == ["ka"]
