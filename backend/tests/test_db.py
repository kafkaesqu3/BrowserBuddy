from browserbuddy import db


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    return {r["name"] for r in rows}


def test_connect_applies_schema(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    tables = _tables(conn)
    assert {
        "history_entries",
        "history_fts",
        "open_tabs",
        "ingest_clients",
        "schema_version",
    } <= tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    db.connect(path).close()
    conn = db.connect(path)  # re-running migrations must not fail
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    conn.close()


def test_fts_stays_in_sync(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://example.com/quantum', 'Quantum Computing Intro', 1000, 'import')"
    )
    conn.commit()
    hit = conn.execute("SELECT rowid FROM history_fts WHERE history_fts MATCH 'quantum'").fetchone()
    assert hit is not None
    conn.close()


def test_fts_sync_on_update_and_delete(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://example.com/a', 'Old Title Zebra', 1000, 'import')"
    )
    conn.execute("UPDATE history_entries SET title = 'New Title Giraffe'")
    conn.commit()

    def match(term):
        return conn.execute(
            "SELECT rowid FROM history_fts WHERE history_fts MATCH ?", (term,)
        ).fetchone()

    assert match("giraffe") is not None
    assert match("zebra") is None
    conn.execute("DELETE FROM history_entries")
    conn.commit()
    assert match("giraffe") is None
    # Raises SQLITE_CORRUPT_VTAB if the external-content index is out of sync.
    conn.execute("INSERT INTO history_fts(history_fts) VALUES ('integrity-check')")
    conn.close()


def test_v2_migration_upgrades_v1_data_intact(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    conn = db.connect(path)  # v1 only
    conn.execute(
        "INSERT INTO history_entries (url, title, visit_time, source)"
        " VALUES ('https://keep.me', 'Keep', 1000, 'import')"
    )
    conn.commit()
    conn.close()
    monkeypatch.undo()
    conn = db.connect(path)  # now applies v2 on top of existing v1 data
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    tables = _tables(conn)
    assert {"sessions", "page_visits", "page_fts"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM history_entries").fetchone()[0] == 1
    conn.close()
