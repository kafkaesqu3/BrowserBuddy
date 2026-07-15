import hmac
import secrets
from pathlib import Path


def get_or_create_secret(data_dir: Path) -> str:
    """Return the shared secret, generating and persisting it on first run."""
    secret_file = data_dir / "secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_urlsafe(32)
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    return secret


def verify_secret(expected: str, provided: str) -> bool:
    return hmac.compare_digest(expected.encode(), provided.encode())
