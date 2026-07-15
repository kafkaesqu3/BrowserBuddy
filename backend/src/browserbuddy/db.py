import sqlite3
from pathlib import Path

# Ordered migration scripts. Append new scripts; never edit shipped ones.
MIGRATIONS: list[str] = [
    # v1: Tier-1 schema — history (+FTS), open tabs, ingest client cursors.
    """
    CREATE TABLE history_entries (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        visit_time INTEGER NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('import', 'sync')),
        UNIQUE (url, visit_time)
    );

    CREATE INDEX idx_history_visit_time ON history_entries (visit_time);

    CREATE VIRTUAL TABLE history_fts USING fts5(
        url, title,
        content='history_entries', content_rowid='id'
    );

    CREATE TRIGGER history_ai AFTER INSERT ON history_entries BEGIN
        INSERT INTO history_fts(rowid, url, title)
        VALUES (new.id, new.url, new.title);
    END;

    CREATE TRIGGER history_ad AFTER DELETE ON history_entries BEGIN
        INSERT INTO history_fts(history_fts, rowid, url, title)
        VALUES ('delete', old.id, old.url, old.title);
    END;

    CREATE TRIGGER history_au AFTER UPDATE ON history_entries BEGIN
        INSERT INTO history_fts(history_fts, rowid, url, title)
        VALUES ('delete', old.id, old.url, old.title);
        INSERT INTO history_fts(rowid, url, title)
        VALUES (new.id, new.url, new.title);
    END;

    CREATE TABLE open_tabs (
        tab_id INTEGER NOT NULL,
        window_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        group_id INTEGER,
        container TEXT,
        opened_at INTEGER,
        last_activated_at INTEGER,
        active_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (window_id, tab_id)
    );

    CREATE TABLE ingest_clients (
        client_id TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the BrowserBuddy database and apply migrations."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row["version"] if row else 0
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(script)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
