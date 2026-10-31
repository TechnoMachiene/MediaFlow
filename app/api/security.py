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
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": subject, "iat": now, "exp": now + timedelta(minutes=ttl_minutes)},
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
