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
    # v2: Tier-2 session capture — sessions and page visits (+FTS).
    """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        client_id TEXT NOT NULL,
        name TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        UNIQUE (client_id, started_at)
    );

    CREATE INDEX idx_sessions_active ON sessions (client_id) WHERE ended_at IS NULL;

    CREATE TABLE page_visits (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions (id),
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        visited_at INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        html_key TEXT,
        container TEXT,
        UNIQUE (session_id, url, visited_at)
    );

    CREATE VIRTUAL TABLE page_fts USING fts5(
        url, title, text,
        content='page_visits', content_rowid='id'
    );

    CREATE TRIGGER page_ai AFTER INSERT ON page_visits BEGIN
        INSERT INTO page_fts(rowid, url, title, text)
        VALUES (new.id, new.url, new.title, new.text);
    END;

    CREATE TRIGGER page_ad AFTER DELETE ON page_visits BEGIN
        INSERT INTO page_fts(page_fts, rowid, url, title, text)
        VALUES ('delete', old.id, old.url, old.title, old.text);
    END;

    CREATE TRIGGER page_au AFTER UPDATE ON page_visits BEGIN
        INSERT INTO page_fts(page_fts, rowid, url, title, text)
        VALUES ('delete', old.id, old.url, old.title, old.text);
        INSERT INTO page_fts(rowid, url, title, text)
        VALUES (new.id, new.url, new.title, new.text);
    END;
    """,
    # v3: Tier-2 link graph — navigation edges and per-page outbound links.
    """
    CREATE TABLE nav_edges (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions (id),
        from_url TEXT,
        to_url TEXT NOT NULL,
        transition TEXT NOT NULL DEFAULT '',
        qualifiers TEXT NOT NULL DEFAULT '',
        query TEXT,
        occurred_at INTEGER NOT NULL,
        UNIQUE (session_id, from_url, to_url, occurred_at)
    );

    CREATE INDEX idx_nav_edges_session ON nav_edges (session_id);

    CREATE TABLE outbound_links (
        id INTEGER PRIMARY KEY,
        page_visit_id INTEGER NOT NULL REFERENCES page_visits (id),
        href TEXT NOT NULL,
        anchor_text TEXT NOT NULL DEFAULT '',
        rel TEXT NOT NULL DEFAULT '',
        clicked INTEGER NOT NULL DEFAULT 0,
        UNIQUE (page_visit_id, href)
    );

    CREATE INDEX idx_outbound_links_page ON outbound_links (page_visit_id);
    """,
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the BrowserBuddy database and apply migrations."""
    # Single-writer by design (one Storage per connection); FastAPI/TestClient may
    # touch it from another thread.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    current = row["version"] or 0
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript() autocommits each statement unless the script opens its
        # own transaction; wrap DDL + version bump so a crash can't leave the
        # schema ahead of schema_version.
        conn.executescript(
            f"BEGIN;\n{script}\n"
            f"INSERT OR REPLACE INTO schema_version (version) VALUES ({version});\n"
            "COMMIT;"
        )
