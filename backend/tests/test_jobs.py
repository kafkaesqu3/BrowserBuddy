import pytest

from browserbuddy import db
from browserbuddy.config import Settings


@pytest.fixture
def jobs(tmp_path):
    from browserbuddy.jobs import JobStore

    settings = Settings(data_dir=tmp_path)
    db.connect(settings.db_path).close()
    return JobStore(settings)


def test_claim_retry_and_stale_completion(jobs):
    from browserbuddy.jobs import JobKey

    key = JobKey("embed_page", 1, "content", "config")
    original = jobs.enqueue(key)
    assert jobs.enqueue(key).id == original.id
    claimed = jobs.claim(now=0)
    assert claimed.id == original.id
    assert jobs.claim(now=0) is None
    assert not jobs.finish(claimed.id, claim_token="stale")
    jobs.fail(
        claimed.id, claim_token=claimed.claim_token, error_code="timeout", transient=True, now=100
    )
    assert jobs.claim(now=5099) is None
    again = jobs.claim(now=5100)
    assert again.claim_token != claimed.claim_token
    assert jobs.finish(again.id, claim_token=again.claim_token)


def test_sixth_failure_is_terminal_and_retry_explicit(jobs):
    from browserbuddy.jobs import JobKey

    job = jobs.enqueue(JobKey("embed_page", 1, "v", "c"))
    now = 0
    for attempt, delay in enumerate([5000, 30000, 120000, 600000, 3600000, 0], 1):
        claimed = jobs.claim(now=now)
        assert claimed.attempt == attempt
        jobs.fail(
            job.id, claim_token=claimed.claim_token, error_code="timeout", transient=True, now=now
        )
        now += delay
    assert jobs.get(job.id).state == "failed"
    assert jobs.claim(now=now) is None
    assert jobs.retry(job.id).state == "pending"
    with pytest.raises(ValueError):
        jobs.retry(job.id)


def test_recovery_and_automatic_digest_gating(jobs):
    from browserbuddy.jobs import JobKey

    job = jobs.enqueue(JobKey("digest", 1, "v", "c"))
    assert jobs.claim(now=0) is None
    jobs.enqueue(JobKey("digest", 1, "v", "c"), origin="explicit")
    first = jobs.claim(now=0)
    jobs.recover()
    second = jobs.claim(now=0)
    assert second.id == job.id
    assert not jobs.finish(first.id, claim_token=first.claim_token)
    assert jobs.finish(second.id, claim_token=second.claim_token)
