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


def test_put_uncompressed_stores_raw_bytes_with_bin_suffix(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"\xff\xd8\xfake-jpeg-bytes"
    key = store.put(data, compress=False)
    assert key == hashlib.sha256(data).hexdigest()
    path = store.path_for(key, compressed=False)
    assert path.suffix == ".bin"
    assert path.read_bytes() == data  # not gzipped


def test_get_finds_uncompressed_blob(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"raw screenshot bytes"
    key = store.put(data, compress=False)
    assert store.get(key) == data


def test_path_for_defaults_to_compressed():
    store = BlobStore.__new__(BlobStore)  # no filesystem needed for this check
    store._root = None  # type: ignore[assignment]
    # path_for with no compressed kwarg still resolves the .gz suffix, matching
    # every existing caller (HTML blobs) which never passes the kwarg.
    import pathlib

    store._root = pathlib.Path("/tmp/unused")
    assert store.path_for("abc").suffix == ".gz"
