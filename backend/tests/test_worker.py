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


def test_ws_remains_live_while_worker_waits(client, app):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "secret": app.state.secret, "client_id": "worker-test"})
        assert ws.receive_json()["type"] == "auth_ok"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
