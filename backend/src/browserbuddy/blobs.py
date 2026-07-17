"""Content-addressed blob storage for raw page HTML (and later screenshots).

Keys are the SHA-256 of the UNCOMPRESSED content; identical pages are stored
once. Files are gzipped at rest (raw HTML compresses ~10x). Layout:
<root>/<key[:2]>/<key[2:]>.gz — two-level fan-out keeps directories small.
"""

import gzip
import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self._root / key[:2] / f"{key[2:]}.gz"

    def put(self, data: bytes) -> str:
        """Store data, returning its content key. No-op if already stored."""
        key = hashlib.sha256(data).hexdigest()
        path = self.path_for(key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data))
            tmp.replace(path)  # atomic: readers never see partial writes
        return key

    def get(self, key: str) -> bytes:
        return gzip.decompress(self.path_for(key).read_bytes())
