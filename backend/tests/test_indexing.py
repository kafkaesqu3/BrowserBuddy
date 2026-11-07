from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


def test_atomic_publication_and_claim_fence(tmp_path):
    from browserbuddy.embeddings import Chunk
    from browserbuddy.indexing import publish_index
    from browserbuddy.jobs import JobKey, JobStore
    from browserbuddy.memory import connection, content_version

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "s", 1)
    page = storage.add_page_visit(
        session,
        url="https://test",
        title="T",
        text="abc",
        visited_at=1,
        html_key=None,
        container=None,
    )
    conn.close()
    jobs = JobStore(settings)
    jobs.enqueue(JobKey("embed_page", page, content_version("T", "https://test", "abc"), "c"))
    job = jobs.claim(now=0)
    chunks = [Chunk(text="abc", ordinal=0, kind="body", start=0, end=3)]
    assert not publish_index(
        job.model_copy(update={"claim_token": "wrong"}),
        settings=settings,
        chunks=chunks,
        vectors=[[1.0, 0.0]],
    )
    with connection(settings) as read:
        assert read.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 0
    assert publish_index(job, settings=settings, chunks=chunks, vectors=[[1.0, 0.0]])
    assert jobs.get(job.id).state == "done"
    with connection(settings) as read:
        assert read.execute("SELECT COUNT(*) FROM indexed_documents").fetchone()[0] == 1
