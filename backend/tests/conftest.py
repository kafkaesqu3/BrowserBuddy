import pytest

from browserbuddy import db
from browserbuddy.storage import Storage


@pytest.fixture
def storage(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Storage(conn)
    conn.close()
