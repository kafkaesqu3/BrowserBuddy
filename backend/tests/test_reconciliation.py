from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.storage import Storage


def test_discovery_backfills_and_recovers_committed_capture(tmp_path):
    from browserbuddy.worker import reconcile

    settings = Settings(data_dir=tmp_path, semantic_enabled=True)
    conn = db.connect(settings.db_path)
    storage = Storage(conn)
    session = storage.start_session("test", "research", 1)
    for n in range(205):
        storage.add_page_visit(
            session,
            url=f"https://test/{n}",
            title="Test",
            text="text",
            visited_at=n,
            html_key=None,
            container=None,
        )
    conn.close()
    assert reconcile(settings, now=10) == 205
    assert reconcile(settings, now=20) == 0
    disabled = settings.model_copy(update={"semantic_enabled": False})
    assert reconcile(disabled, now=30) == 0
