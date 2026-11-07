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
