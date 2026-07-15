from browserbuddy.models import TabRecord


def _tab(tab_id, url, **kw):
    return TabRecord(tab_id=tab_id, window_id=1, url=url, **kw)


def test_replace_and_list_open_tabs(storage):
    storage.replace_open_tabs([_tab(1, "https://a.com", container="Research")])
    storage.replace_open_tabs(
        [_tab(1, "https://a.com", container="Research"), _tab(2, "https://b.com")]
    )
    tabs = storage.list_open_tabs()
    assert [t["url"] for t in tabs] == ["https://a.com", "https://b.com"]
    assert tabs[0]["container"] == "Research"
    assert tabs[1]["container"] is None


def test_replace_with_empty_clears(storage):
    storage.replace_open_tabs([_tab(1, "https://a.com")])
    storage.replace_open_tabs([])
    assert storage.list_open_tabs() == []


def test_client_seq_roundtrip(storage):
    assert storage.get_last_seq("ext-1") == 0
    storage.set_last_seq("ext-1", 41)
    storage.set_last_seq("ext-1", 42)
    assert storage.get_last_seq("ext-1") == 42
    assert storage.get_last_seq("other") == 0


def test_last_seq_never_regresses(storage):
    storage.set_last_seq("ext-1", 42)
    storage.set_last_seq("ext-1", 1)
    assert storage.get_last_seq("ext-1") == 42
