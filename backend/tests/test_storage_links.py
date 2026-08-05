from browserbuddy.models import OutboundLink


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


def test_add_page_visit_returns_id_stable_across_duplicate(storage):
    sid, pid = _page(storage)
    assert pid > 0
    again = storage.add_page_visit(
        sid,
        url="https://a.com",
        title="x",
        visited_at=1100,
        text="y",
        html_key=None,
        container=None,
    )
    assert again == pid


def test_outbound_links_insert_dedup_and_unfollowed(storage):
    sid, pid = _page(storage)
    n = storage.add_outbound_links(
        pid,
        [
            OutboundLink(href="https://x.com", text="X", rel="nofollow"),
            OutboundLink(href="https://y.com", text="Y", rel=""),
        ],
    )
    assert n == 2
    assert storage.add_outbound_links(pid, [OutboundLink(href="https://x.com", text="X2")]) == 0
    unfollowed = storage.list_unfollowed_links(sid)
    assert {r["href"] for r in unfollowed} == {"https://x.com", "https://y.com"}


def test_unfollowed_excludes_chrome_links_by_in_main(storage):
    sid, pid = _page(storage)
    storage.add_outbound_links(
        pid,
        [
            OutboundLink(href="https://article.com/deep", text="Content", in_main=True),
            OutboundLink(href="https://site.com/login", text="Log in", in_main=False),
        ],
    )
    hrefs = {r["href"] for r in storage.list_unfollowed_links(sid)}
    assert hrefs == {"https://article.com/deep"}  # chrome link hidden from the digest


def test_mark_link_clicked_marks_catalogued_and_inserts_dynamic(storage):
    sid, pid = _page(storage)
    storage.add_outbound_links(pid, [OutboundLink(href="https://x.com", text="X")])
    assert (
        storage.mark_link_clicked(sid, page_url="https://a.com", href="https://x.com", text="X")
        is True
    )
    assert all(r["href"] != "https://x.com" for r in storage.list_unfollowed_links(sid))
    assert (
        storage.mark_link_clicked(sid, page_url="https://a.com", href="https://z.com", text="Z")
        is True
    )
    assert all(r["href"] != "https://z.com" for r in storage.list_unfollowed_links(sid))


def test_mark_link_clicked_orphan_returns_false(storage):
    sid, _ = _page(storage)
    assert (
        storage.mark_link_clicked(
            sid, page_url="https://never-seen.com", href="https://x.com", text=""
        )
        is False
    )


def test_add_nav_edge_dedup_and_list_order(storage):
    sid, _ = _page(storage)
    assert (
        storage.add_nav_edge(
            sid,
            from_url="https://google.com/search?q=vec",
            to_url="https://a.com",
            transition="link",
            qualifiers="",
            query="vec",
            occurred_at=1200,
        )
        == 1
    )
    assert (
        storage.add_nav_edge(
            sid,
            from_url="https://google.com/search?q=vec",
            to_url="https://a.com",
            transition="link",
            qualifiers="",
            query="vec",
            occurred_at=1200,
        )
        == 0
    )
    storage.add_nav_edge(
        sid,
        from_url="https://a.com",
        to_url="https://b.com",
        transition="link",
        qualifiers="",
        query=None,
        occurred_at=1300,
    )
    edges = storage.list_nav_edges(sid)
    assert [e["to_url"] for e in edges] == ["https://a.com", "https://b.com"]
    assert edges[0]["query"] == "vec"


def test_add_outbound_links_empty_is_noop(storage):
    # executemany on [] is a known SQLite rowcount footgun — pin the contract.
    _sid, pid = _page(storage)
    assert storage.add_outbound_links(pid, []) == 0


def test_unfollowed_links_ordered_by_visit_then_id(storage):
    sid, pid = _page(storage)
    storage.add_outbound_links(
        pid,
        [
            OutboundLink(href="https://first.com", text="1"),
            OutboundLink(href="https://second.com", text="2"),
        ],
    )
    order = [r["href"] for r in storage.list_unfollowed_links(sid)]
    assert order == ["https://first.com", "https://second.com"]  # insertion (o.id) order


def test_nav_edges_same_time_ordered_by_id(storage):
    sid, _ = _page(storage)
    storage.add_nav_edge(
        sid,
        from_url="https://a.com",
        to_url="https://one.com",
        transition="link",
        qualifiers="",
        query=None,
        occurred_at=5000,
    )
    storage.add_nav_edge(
        sid,
        from_url="https://a.com",
        to_url="https://two.com",
        transition="link",
        qualifiers="",
        query=None,
        occurred_at=5000,
    )
    order = [e["to_url"] for e in storage.list_nav_edges(sid)]
    assert order == ["https://one.com", "https://two.com"]  # id tiebreak at equal occurred_at
