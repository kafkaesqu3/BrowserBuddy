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
        [HistoryVisit(url="https://arxiv.org/abs/9", title="Vector DB survey",
                      visit_time=1720000000000)],
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

    result = runner.invoke(
        app, ["import-history", "--profile", str(profile)], env=_env(tmp_path)
    )
    assert result.exit_code == 0
    assert "Imported 1" in result.output


def test_import_history_missing_profile_fails_clearly(tmp_path):
    result = runner.invoke(
        app, ["import-history", "--profile", str(tmp_path / "nope")], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "places.sqlite" in result.output
