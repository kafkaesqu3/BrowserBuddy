def test_link_scoring_filters_before_cap_and_counts_omissions():
    from browserbuddy.link_scoring import score_outbound_links

    links = [
        {
            "href": f"https://other.test/{i:03}",
            "anchor_text": "A useful research paper",
            "page_visit_id": 1,
            "page_url": "https://source.test",
            "in_main": 1,
            "clicked": 0,
            "novel": True,
        }
        for i in range(55)
    ]
    links += [dict(links[0], clicked=1), dict(links[1], href="javascript:bad")]
    result = score_outbound_links(links, visited_urls={links[1]["href"]})
    assert len(result.items) == 50
    assert result.eligible == 53
    assert result.omitted == 3
    assert result.invalid == 1
    assert result.items[0]["href"] == "https://other.test/002"
