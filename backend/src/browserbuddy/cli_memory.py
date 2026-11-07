"""New backend-dependent commands; legacy offline commands stay in cli.py."""

import asyncio
from datetime import datetime

import typer

from browserbuddy.client import MemoryClient
from browserbuddy.config import get_settings
from browserbuddy.memory_models import AskRequest


def parse_time(value: str | None) -> int | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("dates require an explicit timezone")
    return int(parsed.timestamp() * 1000)


def run_client(operation) -> None:
    async def run():
        async with MemoryClient(get_settings()) as client:
            return await operation(client)

    try:
        result = asyncio.run(run())
    except ValueError as exc:
        typer.echo(f"Invalid input: {exc}", err=True)
        raise typer.Exit(2) from exc
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(result.model_dump_json(indent=2))


def register(app: typer.Typer) -> None:
    jobs_app = typer.Typer(invoke_without_command=True)
    index_app = typer.Typer()
    app.add_typer(jobs_app, name="jobs")
    app.add_typer(index_app, name="index")

    @app.command("digest")
    def digest(session_id: int, regenerate: bool = False):
        async def operation(client):
            result = await client.get_session_digest(session_id)
            if regenerate or result.state in ("missing", "stale"):
                return await client.digest(session_id, regenerate=regenerate)
            return result

        run_client(operation)

    @app.command("mcp")
    def mcp():
        from browserbuddy.mcp import run_mcp

        try:
            asyncio.run(run_mcp())
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc

    @app.command("ask")
    def ask(
        question: str,
        since: str | None = None,
        until: str | None = None,
        session: int | None = None,
        json: bool = False,
    ):
        async def operation(client):
            return await client.ask(
                AskRequest(
                    question=question,
                    since=parse_time(since),
                    until=parse_time(until),
                    session_id=session,
                )
            )

        run_client(operation)

    @jobs_app.callback()
    def jobs(ctx: typer.Context, after_id: int = 0, limit: int = 20, json: bool = False):
        if ctx.invoked_subcommand is None:
            run_client(lambda client: client.jobs(after_id=after_id, limit=limit))

    @jobs_app.command("retry")
    def retry(job_id: int):
        run_client(lambda client: client.retry(job_id))

    @index_app.command("prepare")
    def prepare():
        run_client(lambda client: client.prepare())
