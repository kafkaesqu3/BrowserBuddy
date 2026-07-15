from browserbuddy.models import HistoryVisit


def _visit(url, title="", t=1720000000000):
    return HistoryVisit(url=url, title=title, visit_time=t)


def test_insert_and_search(storage):
    n = storage.insert_history_visits(
        [
            _visit("https://arxiv.org/abs/1", "Quantum error correction survey"),
            _visit("https://example.com/cooking", "Pasta recipes"),
        ],
        source="import",
    )
    assert n == 2
    results = storage.search_history("quantum")
    assert len(results) == 1
    assert results[0]["url"] == "https://arxiv.org/abs/1"


def test_duplicate_visits_ignored(storage):
    visits = [_visit("https://example.com", "Example")]
    assert storage.insert_history_visits(visits, source="import") == 1
    assert storage.insert_history_visits(visits, source="sync") == 0


def test_search_survives_fts_special_chars(storage):
    storage.insert_history_visits([_visit("https://example.com", 'C++ "tricks"')], "import")
    # Must not raise an FTS5 syntax error.
    results = storage.search_history('c++ "tricks" OR')
    assert isinstance(results, list)


def test_search_empty_query_returns_empty(storage):
    assert storage.search_history("   ") == []
