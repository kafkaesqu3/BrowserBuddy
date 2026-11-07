from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


def test_snapshot_is_immutable_and_identity_ignores_observation_time(tmp_path):
    from browserbuddy.digests import DigestService, snapshot_session

    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("c", "s", 1)
    page = storage.add_page_visit(
        session,
        url="https://test",
        title="T",
        text="original",
        visited_at=10,
        html_key=None,
        container=None,
    )
    first = snapshot_session(session, settings=settings)
    assert first.source_version == snapshot_session(session, settings=settings).source_version
    service = DigestService(settings)
    requested = service.request_digest(session)
    assert service.request_digest(session).job_id == requested.job_id
    conn.execute("UPDATE page_visits SET text='changed' WHERE id=?", (page,))
    conn.commit()
    assert first.pages[0]["text"] == "original"
    assert first.source_version != snapshot_session(session, settings=settings).source_version
    assert service.request_digest(session).job_id != requested.job_id
    assert storage.get_active_session("c") is not None
    conn.close()


def test_implicit_closure_is_discovered(tmp_path):
    from browserbuddy.digests import reconcile_digests

    settings = Settings(
        data_dir=tmp_path,
        auto_digests=True,
        llm_base_url="http://localhost:9999/v1",
        llm_model="test",
    )
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    storage.start_session("c", "first", 1)
    assert reconcile_digests(settings) == 0
    storage.start_session("c", "second", 2)
    assert reconcile_digests(settings) == 1
    assert reconcile_digests(settings) == 0
    conn.close()
