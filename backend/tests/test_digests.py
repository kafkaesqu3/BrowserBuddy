import asyncio

from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


def test_empty_digest_and_regeneration_retain_previous_output(tmp_path):
    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    session = Storage(conn).start_session("c", "Empty", 1)
    service = DigestService(settings)
    requested = service.request_digest(session)
    jobs = JobStore(settings)
    job = jobs.claim(now=0)
    asyncio.run(generate_digest(job, settings=settings))
    ready = service.get_session_digest(session)
    assert ready.state == "ready"
    assert "No pages captured" in ready.markdown
    again = service.request_digest(session, regenerate=True)
    assert again.job_id == requested.job_id
    assert again.state == "pending"
    assert again.previous_output
    assert again.markdown == ready.markdown
    assert service.request_digest(session, regenerate=True).job_id == requested.job_id
    asyncio.run(generate_digest(jobs.claim(now=0), settings=settings))
    assert service.get_session_digest(session).state == "ready"
    conn.close()


def test_new_snapshot_keeps_previous_ready_artifact(tmp_path):
    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "Research", 1)
    service = DigestService(settings)
    service.request_digest(session)
    asyncio.run(generate_digest(JobStore(settings).claim(now=0), settings=settings))
    storage.end_session("c", 100)
    newer = service.request_digest(session)
    assert newer.state == "pending"
    assert newer.previous_output
    assert newer.stale
    assert "No pages captured" in newer.markdown
    conn.close()


def test_obsolete_configuration_cannot_generate(tmp_path):
    import pytest

    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path, llm_model="A", llm_base_url="http://localhost/v1")
    conn = db.connect(settings.db_path)
    session = Storage(conn).start_session("c", "Research", 1)
    DigestService(settings).request_digest(session)
    job = JobStore(settings).claim(now=0)
    changed = settings.model_copy(update={"llm_model": "B"})
    with pytest.raises(ValueError, match="configuration"):
        asyncio.run(generate_digest(job, settings=changed))
    conn.close()


def test_disable_automatic_generation_prevents_model_repair(tmp_path):
    import pytest
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore
    from browserbuddy.worker import CapabilityUnavailable

    settings = Settings(data_dir=tmp_path, auto_digests=True)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "Research", 1)
    storage.add_page_visit(
        session,
        url="https://test",
        title="T",
        text="Evidence",
        visited_at=1,
        html_key=None,
        container=None,
    )
    DigestService(settings).request_digest(session, origin="automatic")
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        settings.auto_digests = False
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "text": "Missing citation",
                        "status": "answered",
                        "citations": [],
                    },
                )
            ]
        )

    with pytest.raises(CapabilityUnavailable):
        asyncio.run(
            generate_digest(
                JobStore(settings).claim(now=0), settings=settings, model=FunctionModel(respond)
            )
        )
    assert calls == 1
    conn.close()


def test_many_short_pages_fit_serialized_synthesis_budget(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from browserbuddy.digest_generation import render_digest
    from browserbuddy.digests import DigestService, snapshot_session
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "Many", 1)
    summaries = {}
    for at in range(300):
        page = storage.add_page_visit(
            session,
            url=f"https://test/{at}",
            title="Title",
            text="short text",
            visited_at=at,
            html_key=None,
            container=None,
        )
        summaries[str(page)] = {"text": "Short summary."}
    manifest = snapshot_session(session, settings=settings)
    DigestService(settings).request_digest(session)

    def respond(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "text": "Summary",
                        "status": "answered",
                        "citations": [{"source_kind": "page", "source_id": 1}],
                    },
                )
            ]
        )

    text = asyncio.run(
        render_digest(
            JobStore(settings).claim(now=0),
            manifest,
            summaries,
            settings=settings,
            model=FunctionModel(respond),
        )
    )
    assert "Summary" in text
    assert "Pages omitted from final synthesis: 0" not in text
    conn.close()


def test_recovery_after_file_rename_before_database_publication(tmp_path, monkeypatch):
    import pytest

    from browserbuddy import digest_generation
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    session = Storage(conn).start_session("c", "Empty", 1)
    service = DigestService(settings)
    service.request_digest(session)
    jobs = JobStore(settings)
    job = jobs.claim(now=0)
    original = digest_generation.connection

    def crash(settings):
        raise RuntimeError("simulated process crash before DB publication")

    monkeypatch.setattr(digest_generation, "connection", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        digest_generation.publish_digest(job, "No pages captured.", settings=settings)
    assert list((tmp_path / "digests").rglob("*.md"))
    assert service.get_session_digest(session).state == "pending"
    monkeypatch.setattr(digest_generation, "connection", original)
    jobs.recover()
    asyncio.run(digest_generation.generate_digest(jobs.claim(now=0), settings=settings))
    result = service.get_session_digest(session)
    assert result.state == "ready"
    assert (tmp_path / result.markdown_path).is_file()
    conn.close()


def test_digest_continues_after_hundred_pages_without_repeating_model_calls(tmp_path):
    import re

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "Long session", 1)
    for at in range(101):
        storage.add_page_visit(
            session,
            url=f"https://test/{at}",
            title="Title",
            text="Evidence",
            visited_at=at,
            html_key=None,
            container=None,
        )
    service = DigestService(settings)
    service.request_digest(session)
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        match = re.search(r"Page ID: page:(\d+)", str(messages))
        page_id = int(match[1]) if match else 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "text": "Finding",
                        "status": "answered",
                        "citations": [{"source_kind": "page", "source_id": page_id}],
                    },
                )
            ]
        )

    model = FunctionModel(respond)
    jobs = JobStore(settings)
    asyncio.run(generate_digest(jobs.claim(now=0), settings=settings, model=model))
    assert calls == 100
    assert service.get_session_digest(session).state == "pending"
    jobs.recover()
    asyncio.run(generate_digest(jobs.claim(now=10**15), settings=settings, model=model))
    assert calls == 102
    assert service.get_session_digest(session).state == "ready"
    conn.close()


def test_digest_uses_snapshot_and_cited_model_output(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from browserbuddy.digest_generation import generate_digest
    from browserbuddy.digests import DigestService
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "Research", 1)
    page = storage.add_page_visit(
        session,
        url="https://test",
        title="Original title",
        text="Original evidence",
        visited_at=10,
        html_key=None,
        container=None,
    )
    service = DigestService(settings)
    service.request_digest(session)
    conn.execute("UPDATE page_visits SET text='New evidence' WHERE id=?", (page,))
    conn.commit()

    def respond(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "text": "Original finding",
                        "status": "answered",
                        "citations": [{"source_kind": "page", "source_id": page}],
                    },
                )
            ]
        )

    job = JobStore(settings).claim(now=0)
    asyncio.run(generate_digest(job, settings=settings, model=FunctionModel(respond)))
    result = service.get_session_digest(session)
    assert result.stale
    assert "Original finding" in result.markdown
    assert f"page:{page}" in result.markdown
    conn.close()
