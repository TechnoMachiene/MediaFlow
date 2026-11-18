"""The delivery loop: moves due publications to their platform, in the worker.

Delivery is driven from PostgreSQL alone. Publications are few and scheduled for a time, so a
polled table with leases is simpler and safer than a second queue to keep in step.

Each publication is handled in three steps, and no transaction is held open while talking to
the provider:

1. **Claim** (one transaction, `FOR UPDATE SKIP LOCKED`): re-check that it may still go out
   (post still scheduled, draft still approved, connection still active), compose the payload
   from the *current* draft, set `publishing` with a lease, and bump `attempts`. The attempt
   number is the claim token.
2. **Call** the provider: refresh the token if it is about to expire, then publish (or check
   a `processing` post).
3. **Record** (a second transaction): only if the row is still `publishing` with the same
   attempt number. A worker that lost its lease can't overwrite a newer outcome.

A worker that dies between 1 and 3 leaves `publishing` with an expired lease. The outcome is
then unknown: the platform may or may not have the post. An idempotent provider is simply
called again with the same key (the publication id). For any other provider the row becomes
`unknown`, and a person checks the account before retrying, because a double post is worse
than a late one.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.api.database import ApiJob, Publication, PublishingConnection, ScheduledPost
from app.obs.metrics import PUBLISH_OUTCOMES
from app.publishing.base import (
    AuthorizationLost,
    Credentials,
    Payload,
    Provider,
    PublishError,
    Receipt,
)
from app.publishing.compose import compose
from app.publishing.crypto import Cipher

log = logging.getLogger("publishing")

DUE = ("queued", "retrying", "processing")
FINAL = ("published", "failed", "cancelled", "unknown")
UNKNOWN = ("The worker stopped while publishing, so it isn't known whether the platform has "
           "the post. Check the account, then retry or cancel.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class _Claim:
    publication_id: str
    attempt: int
    action: str  # publish | check
    provider: Provider
    connection_id: str
    account_id: str
    options: dict
    credentials: Credentials
    payload: Payload | None
    external_id: str | None
    media: object | None = None  # JobMedia, for providers that post the photo or video


class Deliverer:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        providers: dict[str, Provider],
        cipher: Cipher,
        *,
        lease_s: float = 120.0,
        max_attempts: int = 5,
        backoff_base_s: float = 30.0,
        backoff_cap_s: float = 1800.0,
        check_every_s: float = 30.0,
        refresh_margin_s: float = 120.0,
        batch: int = 50,
        now: Callable[[], datetime] = utcnow,
        media=None,  # app.publishing.media.MediaResolver
        notify: Callable | None = None,  # app.api.notifications.publication_outcome
    ):
        self.sessions, self.providers, self.cipher = session_factory, providers, cipher
        self.media, self.notify = media, notify
        self.lease = timedelta(seconds=lease_s)
        self.max_attempts, self.batch, self.now = max_attempts, batch, now
        self.backoff_base_s, self.backoff_cap_s = backoff_base_s, backoff_cap_s
        self.check_every = timedelta(seconds=check_every_s)
        self.refresh_margin = timedelta(seconds=refresh_margin_s)

    # ── Loop ─────────────────────────────────────────────────────────────────────────────

    def _due(self, now: datetime):
        return or_(
            and_(Publication.status.in_(DUE), Publication.next_attempt_at <= now),
            and_(Publication.status == "publishing", Publication.locked_until < now),
        )

    def run_once(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        now = self.now()
        with self.sessions() as db:
            ids = db.scalars(
                select(Publication.id).where(self._due(now))
                .order_by(Publication.next_attempt_at).limit(self.batch)
            ).all()
        for publication_id in ids:
            outcome = self.deliver(publication_id)
            if outcome:
                counts[outcome] += 1
        return dict(counts)

    def deliver(self, publication_id: str) -> str | None:
        claim, outcome = self._claim(publication_id)
        if claim is None:
            return outcome
        try:
            credentials = self._fresh(claim)
            if claim.action == "check":
                receipt = claim.provider.check(credentials, claim.external_id,
                                               account_id=claim.account_id, options=claim.options)
            elif claim.provider.info.needs_media:
                receipt = claim.provider.publish(credentials, claim.payload,
                                                 account_id=claim.account_id,
                                                 options=claim.options, media=claim.media)
            else:
                receipt = claim.provider.publish(credentials, claim.payload,
                                                 account_id=claim.account_id,
                                                 options=claim.options)
        except PublishError as err:
            return self._record_failure(claim, err)
        except Exception as err:  # a bug or an unmapped library error: treat as transient
            log.exception("provider call failed", extra={"publication_id": publication_id})
            return self._record_failure(claim, _Unexpected(str(err)[:300]))
        return self._record_receipt(claim, receipt)

    # ── Step 1: claim ────────────────────────────────────────────────────────────────────

    def _claim(self, publication_id: str) -> tuple[_Claim | None, str | None]:
        now = self.now()
        with self.sessions.begin() as db:
            pub = db.scalar(select(Publication).where(
                Publication.id == publication_id, self._due(now)
            ).with_for_update(skip_locked=True))
            if pub is None:
                return None, None  # someone else has it, or it is no longer due
            conn = db.get(PublishingConnection, pub.connection_id)
            post = db.get(ScheduledPost, pub.scheduled_post_id)
            job = db.get(ApiJob, post.job_id) if post else None
            provider = self.providers.get(conn.provider) if conn else None

            if post is None or post.status != "scheduled":
                return None, self._stop(pub, "cancelled", "post_cancelled",
                                        "The calendar entry was cancelled", conn)
            if job is None or job.approval_status != "approved" or job.result is None:
                return None, self._stop(pub, "failed", "not_approved",
                                        "The draft is no longer approved", conn)
            if conn is None or conn.status != "active" or not conn.credentials:
                return None, self._stop(pub, "failed", "connection",
                                        "The account is not connected", conn)
            if provider is None:
                return None, self._stop(pub, "failed", "provider_disabled",
                                        f"Provider {conn.provider!r} is not enabled here", conn)

            action = "check" if pub.status == "processing" else "publish"
            if pub.status == "publishing":  # lease expired: the last call's outcome is unknown
                if not provider.info.idempotent:
                    return None, self._stop(pub, "unknown", "unknown_outcome", UNKNOWN, conn)
                log.warning("re-sending after an unknown outcome (idempotent provider)",
                            extra={"publication_id": pub.id})
                action = "check" if pub.external_id else "publish"

            payload, media = None, None
            if action == "publish" and provider.info.needs_media:
                media = self.media.for_job(db, job) if self.media is not None else None
                if media is None:
                    return None, self._stop(pub, "failed", "no_media",
                                            "The original photo or video was deleted, so "
                                            "there's nothing to post. The drafts are kept.", conn)
            if action == "publish":
                try:
                    body, hashtags = compose(job.result["text"], conn.channel)
                except ValueError as err:
                    return None, self._stop(pub, "failed", "nothing_to_post", str(err), conn)
                payload = Payload(
                    idempotency_key=pub.id, channel=conn.channel, body=body, hashtags=hashtags,
                    media_kind=job.media_kind, media_sha256=job.media_sha256, media_ref=None,
                )
            try:
                credentials = self.cipher.open_credentials(conn.credentials)
            except AuthorizationLost as err:
                return None, self._lost_authorization(pub, conn, err)

            pub.attempts += 1
            pub.status, pub.locked_until = "publishing", now + self.lease
            return _Claim(
                publication_id=pub.id, attempt=pub.attempts, action=action, provider=provider,
                connection_id=conn.id, account_id=conn.account_id, options=dict(conn.options),
                credentials=credentials, payload=payload, external_id=pub.external_id,
                media=media,
            ), None

    def _stop(self, pub: Publication, status: str, code: str, message: str,
              conn: PublishingConnection | None) -> str:
        pub.status, pub.error_code, pub.last_error = status, code, message[:500]
        pub.locked_until = pub.next_attempt_at = None
        self._tell(pub)
        self._count(conn.provider if conn else "unknown", code)
        log.info("publication stopped", extra={"publication_id": pub.id, "code": code})
        return code

    def _lost_authorization(self, pub, conn, err) -> str:
        conn.status, conn.last_error = "needs_reauth", str(err)[:500]
        return self._stop(pub, "failed", "authorization", str(err), conn)

    # ── Step 2: fresh credentials ────────────────────────────────────────────────────────

    def _fresh(self, claim: _Claim) -> Credentials:
        expires = _utc(claim.credentials.expires_at)
        if expires is None or expires - self.refresh_margin > self.now():
            return claim.credentials
        fresh = claim.provider.refresh(claim.credentials, account_id=claim.account_id)
        with self.sessions.begin() as db:
            conn = db.get(PublishingConnection, claim.connection_id)
            conn.credentials = self.cipher.seal_credentials(fresh)
            conn.token_expires_at = fresh.expires_at
        return fresh

    # ── Step 3: record ───────────────────────────────────────────────────────────────────

    def _owned(self, db: Session, claim: _Claim) -> Publication | None:
        pub = db.scalar(select(Publication).where(Publication.id == claim.publication_id)
                        .with_for_update())
        if pub is None or pub.status != "publishing" or pub.attempts != claim.attempt:
            log.warning("stale delivery result ignored", extra={
                "publication_id": claim.publication_id, "attempt": claim.attempt})
            return None
        return pub

    def _record_receipt(self, claim: _Claim, receipt: Receipt) -> str:
        now = self.now()
        with self.sessions.begin() as db:
            pub = self._owned(db, claim)
            if pub is None:
                return "stale"
            pub.external_id, pub.external_url = receipt.external_id, receipt.url
            pub.error_code = pub.last_error = None
            pub.locked_until = None
            if receipt.state == "published":
                pub.status, pub.published_at, pub.next_attempt_at = "published", now, None
                self._tell(pub)
            else:
                pub.status, pub.next_attempt_at = "processing", now + self.check_every
        self._count(claim.provider.info.name, receipt.state)
        log.info("publication %s", receipt.state, extra={"publication_id": claim.publication_id})
        return receipt.state

    def _record_failure(self, claim: _Claim, err: PublishError) -> str:
        now = self.now()
        with self.sessions.begin() as db:
            pub = self._owned(db, claim)
            if pub is None:
                return "stale"
            pub.locked_until = None
            pub.error_code, pub.last_error = err.code, str(err)[:500]
            if isinstance(err, AuthorizationLost):
                conn = db.get(PublishingConnection, claim.connection_id)
                conn.status, conn.last_error = "needs_reauth", str(err)[:500]
                pub.status, pub.next_attempt_at = "failed", None
                outcome = "authorization"
            elif err.retryable and pub.attempts < self.max_attempts:
                pub.status = "retrying"
                pub.next_attempt_at = now + timedelta(seconds=self._delay(pub.attempts, err))
                outcome = "retry"
            else:
                pub.status, pub.next_attempt_at = "failed", None
                outcome = "failed"
            if pub.status == "failed":
                self._tell(pub)
        self._count(claim.provider.info.name, outcome if outcome != "failed" else err.code)
        log.warning("publication %s", outcome, extra={
            "publication_id": claim.publication_id, "code": err.code})
        return outcome

    def _tell(self, pub: Publication) -> None:
        """Stage the people's notification in the same transaction as the outcome."""
        if self.notify is None or pub.status not in ("published", "failed", "unknown"):
            return
        db = Session.object_session(pub)
        try:
            with db.begin_nested():
                self.notify(db, pub)
        except Exception:  # a notification must never undo a delivery outcome
            log.exception("could not stage the publication notification",
                          extra={"publication_id": pub.id})

    def _delay(self, attempt: int, err: PublishError) -> float:
        backoff = min(self.backoff_cap_s, self.backoff_base_s * 2 ** (attempt - 1))
        backoff *= random.uniform(0.8, 1.2)  # spread a fleet of retries apart
        return max(backoff, err.retry_after or 0.0)

    @staticmethod
    def _count(provider: str, outcome: str) -> None:
        PUBLISH_OUTCOMES.labels(provider, outcome).inc()


class _Unexpected(PublishError):
    code, retryable = "unexpected", True
