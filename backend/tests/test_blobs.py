import gzip
import hashlib

import pytest

from browserbuddy.blobs import BlobStore


def test_put_get_roundtrip_and_key_is_sha256(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"<html><body>hello research</body></html>"
    key = store.put(data)
    assert key == hashlib.sha256(data).hexdigest()
    assert store.get(key) == data


def test_put_is_idempotent_and_deduplicates(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    key1 = store.put(b"same content")
    key2 = store.put(b"same content")
    assert key1 == key2
    files = list((tmp_path / "blobs").rglob("*.gz"))
    assert len(files) == 1


def test_blobs_are_gzipped_at_rest(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"x" * 100_000
    key = store.put(data)
    path = store.path_for(key)
    assert path.suffix == ".gz"
    assert path.stat().st_size < len(data) / 10
    assert gzip.decompress(path.read_bytes()) == data


def test_get_missing_raises(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    with pytest.raises(FileNotFoundError):
        store.get("0" * 64)
