from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(subject: str, secret: str, ttl_minutes: int) -> str:
    return create_versioned_access_token(subject, secret, ttl_minutes, token_version=0)


def create_versioned_access_token(
    subject: str,
    secret: str,
    ttl_minutes: int,
    *,
    token_version: int,
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "ver": token_version,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        },
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str) -> str | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def decode_versioned_access_token(token: str, secret: str) -> tuple[str, int] | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    subject = payload.get("sub")
    version = payload.get("ver")
    if (
        not isinstance(subject, str)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
    ):
        return None
    return subject, version
