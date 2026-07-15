import hmac
import os
import secrets
from pathlib import Path


def get_or_create_secret(data_dir: Path) -> str:
    """Return the shared secret, generating and persisting it on first run.

    Creation is atomic (O_CREAT | O_EXCL, mode 0o600): concurrent callers
    converge on one secret and it is never readable by other users.
    """
    secret_file = data_dir / "secret"
    if not secret_file.exists():
        new_secret = secrets.token_urlsafe(32)
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process won the race; fall through and read theirs
        else:
            with os.fdopen(fd, "w") as f:
                f.write(new_secret)
            return new_secret
    secret = secret_file.read_text().strip()
    if not secret:
        raise ValueError(f"{secret_file} is empty or corrupted; delete it to regenerate.")
    return secret


def verify_secret(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())
