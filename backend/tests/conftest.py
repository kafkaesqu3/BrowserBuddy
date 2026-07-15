import pytest
from fastapi.testclient import TestClient

from browserbuddy import db
from browserbuddy.storage import Storage


@pytest.fixture
def storage(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Storage(conn)
    conn.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERBUDDY_DATA_DIR", str(tmp_path))
    from browserbuddy.app import create_app
    from browserbuddy.config import get_settings

    return create_app(get_settings())


@pytest.fixture
def client(app):
    return TestClient(app)
