import asyncio

import pytest

from browserbuddy import db
from browserbuddy.config import Settings


def test_backend_owner_excludes_second_owner(tmp_path):
    from browserbuddy.ownership import BackendOwner

    with (
        BackendOwner(tmp_path),
        pytest.raises(RuntimeError, match="already running"),
        BackendOwner(tmp_path),
    ):
        pytest.fail("second owner acquired the lock")
    with BackendOwner(tmp_path):
        pass


def test_worker_claims_recovers_and_stops(tmp_path):
    from browserbuddy.jobs import JobKey, JobStore
    from browserbuddy.worker import Worker

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    db.connect(settings.db_path).close()
    jobs = JobStore(settings)
    job = jobs.enqueue(JobKey("embed_page", 1, "v", "c"))
    jobs.claim(now=0)

    async def run():
        completed = asyncio.Event()

        async def handle(item):
            jobs.finish(item.id, claim_token=item.claim_token)
            completed.set()

        worker = Worker(settings, handler=handle)
        worker.start()
        await asyncio.wait_for(completed.wait(), 2)
        await worker.stop()
        assert jobs.get(job.id).state == "done"
        assert worker.tasks == []

    asyncio.run(run())


def test_ws_acknowledges_capture_while_inference_is_blocked(client, app):
    import threading

    from browserbuddy.embeddings import INFERENCE
    from browserbuddy.jobs import JobKey, JobStore

    entered, release = threading.Event(), threading.Event()

    def inference():
        entered.set()
        release.wait(3)

    async def handle(job):
        await asyncio.wrap_future(INFERENCE.submit(inference))

    app.state.worker.handler = handle
    JobStore(app.state.settings).enqueue(JobKey("embed_page", 1, "v", "c"))
    assert entered.wait(2)
    watchdog = threading.Timer(2, release.set)
    watchdog.start()
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "secret": app.state.secret, "client_id": "worker-test"})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json(
                {
                    "v": 1,
                    "seq": 1,
                    "event": {
                        "type": "session_start",
                        "name": "While busy",
                        "started_at": 1,
                    },
                }
            )
            assert ws.receive_json() == {"type": "ack", "seq": 1}
            ws.send_json(
                {
                    "v": 1,
                    "seq": 2,
                    "event": {
                        "type": "page_capture",
                        "url": "https://t",
                        "text": "text",
                        "visited_at": 2,
                    },
                }
            )
            assert ws.receive_json() == {"type": "ack", "seq": 2}
            assert not release.is_set()
    finally:
        release.set()
        watchdog.cancel()


def test_provider_auth_failure_is_terminal(tmp_path):
    from pydantic_ai.exceptions import ModelHTTPError

    from browserbuddy.jobs import JobKey, JobStore
    from browserbuddy.worker import Worker

    settings = Settings(data_dir=tmp_path)
    db.connect(settings.db_path).close()
    jobs = JobStore(settings)
    job = jobs.enqueue(JobKey("embed_page", 1, "v", "c"))

    async def handler(job):
        raise ModelHTTPError(401, "test-model", "secret provider payload")

    asyncio.run(Worker(settings, handler=handler).run_once())
    failed = jobs.get(job.id)
    assert failed.state == "failed"
    assert "secret" not in failed.model_dump_json()
