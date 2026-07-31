import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import typer

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret
from browserbuddy.config import get_settings
from browserbuddy.importer import firefox
from browserbuddy.storage import Storage

app = typer.Typer(help="BrowserBuddy — your browser's research historian.")


def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _resolve_session(rows: list, selector: str | None):
    """Pick a session row by id, exact name, or (default) the newest."""
    if selector is None:
        return rows[0] if rows else None
    if selector.isdigit():
        return next((r for r in rows if r["id"] == int(selector)), None)
    return next((r for r in rows if r["name"] == selector), None)


@app.command()
def serve() -> None:
    """Run the ingest server (foreground)."""
    import uvicorn

    from browserbuddy.app import create_app

    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


@app.command()
def secret() -> None:
    """Print the shared secret to paste into the extension's options page."""
    typer.echo(get_or_create_secret(get_settings().data_dir))


@app.command("import-history")
def import_history(
    profile: Path | None = typer.Option(  # noqa: B008 — Typer requires Option() in defaults
        None, help="Firefox profile directory (default: auto-detect)."
    ),
) -> None:
    """Backfill browser history from Firefox's places.sqlite."""
    if profile is None:
        try:
            profile = firefox.find_default_profile(firefox.firefox_root())
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
    places = profile / "places.sqlite"
    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        count = firefox.import_places(places, Storage(conn))
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except sqlite3.DatabaseError as exc:
        typer.echo(
            f"Error: {places} is corrupt or not a SQLite database ({exc}). "
            "If Firefox is running, close it and retry.",
            err=True,
        )
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(f"Imported {count} new history entries from {places}")


@app.command()
def sessions() -> None:
    """List research sessions (newest first)."""
    conn = db.connect(get_settings().db_path)
    try:
        rows = Storage(conn).list_sessions()
    finally:
        conn.close()
    if not rows:
        typer.echo("No sessions recorded yet.")
        return
    for row in rows:
        state = "active" if row["ended_at"] is None else _fmt_time(row["ended_at"])
        pages = f"{row['page_count']} page" + ("" if row["page_count"] == 1 else "s")
        typer.echo(f"{_fmt_time(row['started_at'])} → {state}  {row['name']}  ({pages})")


@app.command()
def search(
    query: str,
    limit: int = typer.Option(20, help="Maximum results (capped at 200)."),
) -> None:
    """Full-text search over history and captured session pages."""
    conn = db.connect(get_settings().db_path)
    try:
        storage = Storage(conn)
        history = storage.search_history(query, limit=limit)
        pages = storage.search_pages(query, limit=limit)
    finally:
        conn.close()
    if not history and not pages:
        typer.echo("No results.")
        return
    for row in history:
        typer.echo(f"{_fmt_time(row['visit_time'])}  {row['title'] or '(untitled)'}")
        typer.echo(f"                  {row['url']}")
    if pages:
        typer.echo("-- session pages --")
        for row in pages:
            typer.echo(
                f"{_fmt_time(row['visited_at'])}  {row['title'] or '(untitled)'}"
                f"  [{row['session_name']}]"
            )
            typer.echo(f"                  {row['url']}")


@app.command()
def links(
    session: str | None = typer.Argument(  # noqa: B008 — Typer requires Argument() in defaults
        None, help="Session id or name (default: newest)."
    ),
) -> None:
    """Show a session's navigation edges and notable un-followed outbound links."""
    conn = db.connect(get_settings().db_path)
    try:
        storage = Storage(conn)
        rows = storage.list_sessions(limit=200)
        target = _resolve_session(rows, session)
        if target is None:
            typer.echo("No matching session." if session else "No sessions recorded yet.")
            return
        edges = storage.list_nav_edges(target["id"])
        unfollowed = storage.list_unfollowed_links(target["id"])
    finally:
        conn.close()
    typer.echo(f"Session: {target['name']} (id {target['id']})")
    typer.echo("-- navigation edges --")
    if not edges:
        typer.echo("  (none)")
    for edge in edges:
        query = f'  q="{edge["query"]}"' if edge["query"] else ""
        origin = edge["from_url"] or "(start)"
        typer.echo(f"  {origin} → {edge['to_url']}  [{edge['transition'] or '?'}]{query}")
    typer.echo("-- notable un-followed links --")
    if not unfollowed:
        typer.echo("  (none)")
    for link in unfollowed:
        label = f"  — {link['anchor_text']}" if link["anchor_text"] else ""
        typer.echo(f"  {link['href']}{label}")


@app.command()
def tabs() -> None:
    """Show the current open-tab inventory (as last synced by the extension)."""
    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        rows = Storage(conn).list_open_tabs()
    finally:
        conn.close()
    if not rows:
        typer.echo("No open tabs recorded yet. Is the extension connected?")
        return
    for row in rows:
        container = f" [{row['container']}]" if row["container"] else ""
        typer.echo(
            f"w{row['window_id']} t{row['tab_id']}{container}  {row['title'] or '(untitled)'}"
        )
        typer.echo(f"        {row['url']}")


if __name__ == "__main__":
    app()
