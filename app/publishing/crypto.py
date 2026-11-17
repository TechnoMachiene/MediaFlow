"""Encryption at rest for provider credentials and PKCE verifiers.

Fernet (AES-128-CBC + HMAC-SHA256) with a key from PUBLISHING_KEY, or, when that is unset,
derived from JWT_SECRET with HKDF under a dedicated label. Production should set
PUBLISHING_KEY so the two secrets can rotate independently (rotating it means reconnecting).
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.publishing.base import AuthorizationLost, Credentials


class Cipher:
    def __init__(self, *, publishing_key: str = "", jwt_secret: str = ""):
        if publishing_key:
            key = publishing_key.encode()
        else:
            raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b"mediaflow publishing credentials v1").derive(jwt_secret.encode())
            key = base64.urlsafe_b64encode(raw)
        self._fernet = Fernet(key)

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            # Wrong or rotated key: the stored grant is unusable, which is a reconnect.
            raise AuthorizationLost("Stored credentials can't be decrypted; reconnect") from None

    def seal_credentials(self, credentials: Credentials) -> str:
        data = asdict(credentials)
        if credentials.expires_at:
            data["expires_at"] = credentials.expires_at.isoformat()
        return self.seal(json.dumps(data))

    def open_credentials(self, token: str) -> Credentials:
        data = json.loads(self.open(token))
        if data.get("expires_at"):
            data["expires_at"] = datetime.fromisoformat(data["expires_at"])
        return Credentials(**data)
