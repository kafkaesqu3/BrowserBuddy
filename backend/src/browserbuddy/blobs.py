"""Content-addressed blob storage for raw page HTML and screenshots.

Keys are the SHA-256 of the UNCOMPRESSED content; identical pages/images are
stored once. Text blobs (HTML) are gzipped at rest (~10x). Image blobs
(JPEG screenshots) are already compressed, so gzip would cost CPU for ~0
size benefit — those are stored raw and use a distinct `.bin` suffix so the
two variants for the same key (a caller could theoretically compress the
same bytes once and not another time) never collide on disk. Layout:
<root>/<key[:2]>/<key[2:]>.gz|.bin — two-level fan-out keeps directories small.
"""

import gzip
import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str, *, compressed: bool = True) -> Path:
        suffix = ".gz" if compressed else ".bin"
        return self._root / key[:2] / f"{key[2:]}{suffix}"

    def put(self, data: bytes, *, compress: bool = True) -> str:
        """Store data, returning its content key. No-op if already stored."""
        key = hashlib.sha256(data).hexdigest()
        path = self.path_for(key, compressed=compress)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(gzip.compress(data) if compress else data)
            tmp.replace(path)  # atomic: readers never see partial writes
        return key

    def get(self, key: str) -> bytes:
        gz_path = self.path_for(key, compressed=True)
        if gz_path.exists():
            return gzip.decompress(gz_path.read_bytes())
        return self.path_for(key, compressed=False).read_bytes()
