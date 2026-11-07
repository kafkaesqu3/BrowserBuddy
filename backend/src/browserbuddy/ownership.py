"""OS-held ownership for a backend data directory."""

import os
from pathlib import Path


class BackendOwner:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "backend.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError(f"Backend already running for {self.path.parent}") from exc
        return self

    def __exit__(self, *_):
        self.file.close()
