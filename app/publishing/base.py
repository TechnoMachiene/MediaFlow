"""The provider contract. Every platform integration implements `Provider`, and nothing else
in the app knows which platform it is talking to.

Errors are part of the contract: a provider must translate every failure into one of the
`PublishError` subclasses below, because the delivery loop decides retry vs. stop vs.
reconnect from the type alone (docs/PUBLISHING.md, "Failure handling").
"""

from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


def pkce_s256(verifier: str) -> str:
    """The PKCE code challenge for a verifier (RFC 7636, method S256)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ── Data passed across the boundary ─────────────────────────────────────────────────────────


@dataclass
class Credentials:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Grant:
    """What a finished authorization yields."""

    account_id: str
    account_name: str
    credentials: Credentials


@dataclass(frozen=True)
class Payload:
    """One post, already composed for the connection's channel."""

    idempotency_key: str  # the publication id: stable across every retry of this delivery
    channel: str
    body: str
    hashtags: tuple[str, ...]
    media_kind: str
    media_sha256: str | None
    media_ref: str | None  # storage reference; providers that upload bytes materialize it


@dataclass(frozen=True)
class Receipt:
    """`published` is final. `processing` means the platform accepted the post and will finish
    it asynchronously (video transcoding, review): the loop calls `check` later."""

    state: Literal["published", "processing"]
    external_id: str
    url: str | None = None


# ── Failures ────────────────────────────────────────────────────────────────────────────────


class PublishError(Exception):
    code = "error"
    retryable = False

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(PublishError):
    """5xx, timeouts, connection errors. Retried with backoff."""

    code, retryable = "unavailable", True


class ProviderRateLimited(PublishError):
    """429 or a quota header. Retried no sooner than `retry_after`."""

    code, retryable = "rate_limited", True


class ContentRejected(PublishError):
    """The platform refused this post (policy, format, length). Retrying won't help."""

    code = "rejected"


class AuthorizationLost(PublishError):
    """Token revoked or expired beyond refresh. The connection needs to be reconnected."""

    code = "authorization"


class AuthorizationDenied(PublishError):
    """The person declined on the consent screen."""

    code = "denied"


# ── The interface ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    display_name: str
    sandbox: bool
    channels: tuple[str, ...]
    # Safe to re-send the same idempotency_key after an unknown outcome (crash mid-call)?
    # If False, an unknown outcome is surfaced to a person instead of risking a double post.
    idempotent: bool
    # Provider-specific choices offered when connecting: {option: [allowed values]}.
    options: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Posts the job's photo or video (the delivery loop then passes `media` to `publish`).
    needs_media: bool = False
    # The account belongs to the whole installation (credentials in .env), so only a platform
    # admin may connect it, never a workspace admin.
    operator_only: bool = False


class Provider(ABC):
    info: ProviderInfo

    @abstractmethod
    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          options: dict) -> str:
        """Where to send the admin to grant access (OAuth 2.0 authorization code + PKCE S256)."""

    @abstractmethod
    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str,
                      options: dict, channel: str) -> Grant:
        """Trade the authorization code for credentials. Raise AuthorizationDenied on refusal."""

    @abstractmethod
    def refresh(self, credentials: Credentials, *, account_id: str) -> Credentials:
        """New credentials before `expires_at`. Raise AuthorizationLost if refresh is refused."""

    @abstractmethod
    def publish(self, credentials: Credentials, payload: Payload, *,
                account_id: str, options: dict, media=None) -> Receipt:
        """Send the post. Must honour `payload.idempotency_key` if `info.idempotent`.
        `media` (an `app.publishing.media.JobMedia`) is given when `info.needs_media`; None
        then means the original is gone."""

    @abstractmethod
    def check(self, credentials: Credentials, external_id: str, *,
              account_id: str, options: dict) -> Receipt:
        """Current state of a post that was `processing`."""

    def revoke(self, credentials: Credentials, *, account_id: str) -> None:  # noqa: B027
        """Best effort, on disconnect. Default: nothing to call."""
