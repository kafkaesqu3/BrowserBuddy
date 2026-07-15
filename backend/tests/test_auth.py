from browserbuddy.auth import get_or_create_secret, verify_secret


def test_secret_created_once_and_persisted(tmp_path):
    first = get_or_create_secret(tmp_path)
    second = get_or_create_secret(tmp_path)
    assert first == second
    assert len(first) >= 32
    secret_file = tmp_path / "secret"
    assert secret_file.exists()
    assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_verify_secret():
    assert verify_secret("abc", "abc")
    assert not verify_secret("abc", "abd")
    assert not verify_secret("abc", "")
