import sqlite3

from typer.testing import CliRunner

from browserbuddy.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {"BROWSERBUDDY_DATA_DIR": str(tmp_path)}


def _seed_history(tmp_path):
    from browserbuddy import db
    from browserbuddy.models import HistoryVisit
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    Storage(conn).insert_history_visits(
        [
            HistoryVisit(
                url="https://arxiv.org/abs/9", title="Vector DB survey", visit_time=1720000000000
            )
        ],
        source="import",
    )
    conn.close()


def test_secret_prints_secret(tmp_path):
    result = runner.invoke(app, ["secret"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert (tmp_path / "secret").read_text().strip() in result.output


def test_search_finds_seeded_history(tmp_path):
    _seed_history(tmp_path)
    result = runner.invoke(app, ["search", "vector"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "https://arxiv.org/abs/9" in result.output


def test_search_no_results(tmp_path):
    _seed_history(tmp_path)
    result = runner.invoke(app, ["search", "zebra"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No results" in result.output


def test_tabs_empty(tmp_path):
    result = runner.invoke(app, ["tabs"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No open tabs" in result.output


def test_import_history_with_explicit_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    conn = sqlite3.connect(profile / "places.sqlite")
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_historyvisits (
            id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER
        );
        INSERT INTO moz_places VALUES (1, 'https://a.com', 'A');
        INSERT INTO moz_historyvisits VALUES (1, 1, 1720000000000000);
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["import-history", "--profile", str(profile)], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "Imported 1" in result.output


def test_import_history_missing_profile_fails_clearly(tmp_path):
    result = runner.invoke(
        app, ["import-history", "--profile", str(tmp_path / "nope")], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "places.sqlite" in result.output


def test_tabs_with_data(tmp_path):
    from browserbuddy import db
    from browserbuddy.models import TabRecord
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    Storage(conn).replace_open_tabs(
        [TabRecord(tab_id=1, window_id=1, url="https://a.com", title="A", container="Work")]
    )
    conn.close()
    result = runner.invoke(app, ["tabs"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "w1 t1" in result.output
    assert "[Work]" in result.output
    assert "https://a.com" in result.output


def test_search_output_includes_title_and_timestamp(tmp_path):
    _seed_history(tmp_path)
    result = runner.invoke(app, ["search", "vector"], env=_env(tmp_path))
    assert "Vector DB survey" in result.output
    assert "2024-07-03" in result.output  # 1720000000000 ms = 2024-07-03T09:46:40Z


def test_import_history_corrupt_db_fails_clearly(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "places.sqlite").write_bytes(b"this is not a sqlite database")
    result = runner.invoke(app, ["import-history", "--profile", str(profile)], env=_env(tmp_path))
    assert result.exit_code == 1
    assert "corrupt" in result.output.lower()


def _seed_session(tmp_path):
    from browserbuddy import db
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    s = Storage(conn)
    sid = s.start_session("ext-1", "vector research", started_at=1720000000000)
    s.add_page_visit(
        sid,
        url="https://arxiv.org/abs/8",
        title="HNSW explained",
        visited_at=1720000001000,
        text="hierarchical navigable small worlds",
        html_key=None,
        container=None,
    )
    s.end_session("ext-1", ended_at=1720000100000)
    conn.close()


def test_sessions_lists_sessions(tmp_path):
    _seed_session(tmp_path)
    result = runner.invoke(app, ["sessions"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "vector research" in result.output
    assert "1 page" in result.output


def test_sessions_empty(tmp_path):
    result = runner.invoke(app, ["sessions"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_search_includes_session_pages(tmp_path):
    _seed_session(tmp_path)
    result = runner.invoke(app, ["search", "navigable"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "https://arxiv.org/abs/8" in result.output
    assert "vector research" in result.output
