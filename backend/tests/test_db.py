from browserbuddy import db


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {r["name"] for r in rows}


def test_connect_applies_schema(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    tables = _tables(conn)
    assert {"history_entries", "open_tabs", "ingest_clients", "schema_version"} <= tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
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
    hit = conn.execute(
        "SELECT rowid FROM history_fts WHERE history_fts MATCH 'quantum'"
    ).fetchone()
    assert hit is not None
    conn.close()
