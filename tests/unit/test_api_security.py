import pytest

from app.api.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

pytestmark = pytest.mark.unit


def test_password_hash_and_jwt_roundtrip():
    hashed = hash_password("a-long-password-for-tests")
    assert hashed != "a-long-password-for-tests"
    assert verify_password("a-long-password-for-tests", hashed)
    assert not verify_password("wrong-password", hashed)

    token = create_access_token("user-123", "s" * 32, 30)
    assert decode_access_token(token, "s" * 32) == "user-123"
    assert decode_access_token(token, "different-secret" * 3) is None
    assert decode_access_token("not-a-token", "s" * 32) is None
