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


def test_encoder_generation_is_bound_after_waiting_for_inference(tmp_path):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from browserbuddy.embeddings import INFERENCE, Chunk, prepared_config
    from browserbuddy.indexing import index_document
    from browserbuddy.jobs import JobKey, JobStore
    from browserbuddy.memory import content_version
    from browserbuddy.worker import embedding_identity

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "s", 1)
    page = storage.add_page_visit(
        session, url="https://t", title="T", text="abc", visited_at=1, html_key=None, container=None
    )
    config = {"model": settings.embedding_model, "requested_revision": "main", "revision": "A"}
    path = tmp_path / "embedding.json"
    path.write_text(json.dumps(config))
    jobs = JobStore(settings)
    jobs.enqueue(
        JobKey(
            "embed_page",
            page,
            content_version("T", "https://t", "abc"),
            embedding_identity(settings),
        )
    )
    queued, release = threading.Event(), threading.Event()

    class Encoder:
        def load(self):
            self.config = prepared_config(settings)

        def document(self, **kwargs):
            return [Chunk("abc", 0, "body", 0, 3)], [[1.0, 0.0]]

    original_submit = INFERENCE.submit
    # Boundary synchronization reveals a config change while inference is queued.
    import unittest.mock

    def submit(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        queued.set()
        return future

    blocking = original_submit(release.wait)
    try:
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            unittest.mock.patch.object(INFERENCE, "submit", side_effect=submit),
        ):
            future = executor.submit(
                index_document, jobs.claim(now=0), settings=settings, embedder=Encoder()
            )
            assert queued.wait(2)
            path.write_text(json.dumps({**config, "revision": "B"}))
            release.set()
            future.result(timeout=2)
    finally:
        release.set()
        blocking.result(timeout=2)
    assert conn.execute("SELECT COUNT(*) FROM indexed_documents").fetchone()[0] == 0
    conn.close()
