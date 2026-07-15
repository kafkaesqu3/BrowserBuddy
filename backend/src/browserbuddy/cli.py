from datetime import UTC, datetime
from pathlib import Path

import typer

from browserbuddy import db
from browserbuddy.auth import get_or_create_secret
from browserbuddy.config import get_settings
from browserbuddy.importer import firefox
from browserbuddy.storage import Storage

app = typer.Typer(help="BrowserBuddy — your browser's research historian.")


def _storage() -> Storage:
    settings = get_settings()
    return Storage(db.connect(settings.db_path))


def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


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
    try:
        count = firefox.import_places(places, _storage())
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Imported {count} new history entries from {places}")


@app.command()
def search(
    query: str,
    limit: int = typer.Option(20, help="Maximum results."),
) -> None:
    """Full-text search over captured browser history."""
    results = _storage().search_history(query, limit=limit)
    if not results:
        typer.echo("No results.")
        return
    for row in results:
        typer.echo(f"{_fmt_time(row['visit_time'])}  {row['title'] or '(untitled)'}")
        typer.echo(f"                  {row['url']}")


@app.command()
def tabs() -> None:
    """Show the current open-tab inventory (as last synced by the extension)."""
    rows = _storage().list_open_tabs()
    if not rows:
        typer.echo("No open tabs recorded yet. Is the extension connected?")
        return
    for row in rows:
        container = f" [{row['container']}]" if row["container"] else ""
        typer.echo(
            f"w{row['window_id']} t{row['tab_id']}{container}  "
            f"{row['title'] or '(untitled)'}"
        )
        typer.echo(f"        {row['url']}")


if __name__ == "__main__":
    app()
