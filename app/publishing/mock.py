"""The sandbox provider: a pretend social platform that behaves like a real one.

It runs a real OAuth shape (consent screen, authorization code, PKCE S256 check, expiring
access tokens with refresh), records what it "published" and every request it received
(`sandbox_posts`, `sandbox_requests`), and honours idempotency keys. Each connection picks a
`behavior`, so every failure path in docs/PUBLISHING.md can be exercised on purpose:

    succeed       published at once
    flaky         503 twice, then published (retry with backoff)
    rate_limited  429 with Retry-After once, then published
    processing    accepted as processing; published on the next status check (async delivery)
    reject        refused for policy (permanent failure)
    expire        token already expired and refresh refused (connection needs reauth)

Nothing leaves the installation.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.database import SandboxPost, SandboxRequest
from app.publishing.base import (
    AuthorizationDenied,
    AuthorizationLost,
    ContentRejected,
    Credentials,
    Grant,
    Payload,
    Provider,
    ProviderInfo,
    ProviderRateLimited,
    ProviderUnavailable,
    Receipt,
    pkce_s256,
)

BEHAVIORS = ("succeed", "flaky", "rate_limited", "processing", "reject", "expire")
CHANNELS = ("instagram", "facebook", "tiktok")
TOKEN_TTL = timedelta(hours=1)


def issue_code(code_challenge: str) -> str:
    """The sandbox authorization server's code: carries the challenge it must be redeemed with."""
    raw = json.dumps({"c": code_challenge, "n": secrets.token_urlsafe(12)}).encode()
    return "sbx." + base64.urlsafe_b64encode(raw).decode()


class MockProvider(Provider):
    info = ProviderInfo(
        name="mock", display_name="Sandbox", sandbox=True, channels=CHANNELS, idempotent=True,
        options={"behavior": BEHAVIORS},
    )

    def __init__(self, session_factory: Callable[[], Session]):
        self.sessions = session_factory

    def _log(self, account_id: str, kind: str, outcome: str, key: str | None = None) -> None:
        with self.sessions.begin() as db:
            db.add(SandboxRequest(account_id=account_id, kind=kind, outcome=outcome,
                                  idempotency_key=key))

    def _publish_requests(self, key: str) -> int:
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(SandboxRequest).where(
                SandboxRequest.idempotency_key == key, SandboxRequest.kind == "publish"))

    # ── OAuth ────────────────────────────────────────────────────────────────────────────

    def authorization_url(self, *, state, code_challenge, redirect_uri, options) -> str:
        # The sandbox consent screen is a page of the web app (#/publishing/sandbox).
        return f"/#/publishing/sandbox?state={state}&challenge={code_challenge}"

    def exchange_code(self, *, code, code_verifier, redirect_uri, options, channel="") -> Grant:
        if code == "access_denied":
            raise AuthorizationDenied("Access was not granted on the sandbox consent screen")
        try:
            claim = json.loads(base64.urlsafe_b64decode(code.removeprefix("sbx.")))
        except ValueError:
            raise AuthorizationDenied("Unknown authorization code") from None
        if claim.get("c") != pkce_s256(code_verifier):
            raise AuthorizationDenied("PKCE verification failed")
        account_id = "sbx_" + secrets.token_hex(6)
        expires = datetime.now(timezone.utc) + (
            -timedelta(minutes=1) if options.get("behavior") == "expire" else TOKEN_TTL)
        return Grant(
            account_id=account_id,
            account_name=f"Sandbox {account_id[-4:]}",
            credentials=Credentials(
                access_token="sbx-at-" + secrets.token_urlsafe(16),
                refresh_token="sbx-rt-" + secrets.token_urlsafe(16), expires_at=expires,
                extra={"refresh_refused": True} if options.get("behavior") == "expire" else {},
            ),
        )

    def refresh(self, credentials, *, account_id) -> Credentials:
        if not (credentials.refresh_token or "").startswith("sbx-rt-") or \
                credentials.extra.get("refresh_refused"):
            self._log(account_id, "refresh", "refused")
            raise AuthorizationLost("The sandbox refused to refresh the token; reconnect")
        self._log(account_id, "refresh", "ok")
        return Credentials(
            access_token="sbx-at-" + secrets.token_urlsafe(16),
            refresh_token=credentials.refresh_token,
            expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
        )

    # ── Posting ──────────────────────────────────────────────────────────────────────────

    def publish(self, credentials, payload: Payload, *, account_id, options, media=None) -> Receipt:
        key = payload.idempotency_key
        behavior = options.get("behavior", "succeed")
        with self.sessions() as db:
            existing = db.scalar(select(SandboxPost).where(SandboxPost.idempotency_key == key))
        if existing is not None:  # a retry of something we already have: same post, no dup
            self._log(account_id, "publish", "duplicate", key)
            return self._receipt(existing)
        earlier = self._publish_requests(key)
        if behavior == "expire":
            self._log(account_id, "publish", "401", key)
            raise AuthorizationLost("Sandbox: access token expired")
        if behavior == "flaky" and earlier < 2:
            self._log(account_id, "publish", "503", key)
            raise ProviderUnavailable("Sandbox: 503 Service Unavailable")
        if behavior == "rate_limited" and earlier < 1:
            self._log(account_id, "publish", "429", key)
            raise ProviderRateLimited("Sandbox: 429 Too Many Requests", retry_after=2.0)
        if behavior == "reject":
            self._log(account_id, "publish", "rejected", key)
            raise ContentRejected("Sandbox: this post breaks the (pretend) community guidelines")
        post = SandboxPost(
            external_id="sbxpost_" + secrets.token_hex(8), account_id=account_id,
            idempotency_key=key, channel=payload.channel, body=payload.body,
            hashtags=list(payload.hashtags), media_sha256=payload.media_sha256,
            state="processing" if behavior == "processing" else "published",
        )
        with self.sessions.begin() as db:
            db.add(post)
        self._log(account_id, "publish", post.state, key)
        return self._receipt(post)

    def check(self, credentials, external_id, *, account_id, options) -> Receipt:
        with self.sessions.begin() as db:
            post = db.get(SandboxPost, external_id)
            if post is None:
                raise ContentRejected("Sandbox: post not found")
            post.state = "published"  # finishes on the first check after it was accepted
            receipt = self._receipt(post)
        self._log(account_id, "check", receipt.state, post.idempotency_key)
        return receipt

    @staticmethod
    def _receipt(post: SandboxPost) -> Receipt:
        return Receipt(state=post.state, external_id=post.external_id,
                       url=f"/#/publishing/sandbox-post/{post.external_id}")
