"""Publishing API: connect accounts (OAuth), send scheduled posts to them, follow delivery.

Who may do what:
  * Workspace admins connect and disconnect accounts. Everyone in the workspace can see which
    accounts exist (names only; credentials never leave the server).
  * Whoever may manage a calendar entry (its owner, or an admin) chooses where it publishes,
    retries a failed delivery, or cancels a pending one.
  * Only approved drafts on the calendar can be published, and the delivery loop checks that
    again at send time (app/publishing/delivery.py).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api import audit
from app.api.database import (
    ApiJob,
    Publication,
    PublishingAuthState,
    PublishingConnection,
    SandboxPost,
    SandboxRequest,
    ScheduledPost,
)
from app.api.dependencies import CurrentMember, DbSession, Member
from app.api.roles import is_admin
from app.api.routes import _workspace_post
from app.api.schemas import (
    AuthorizeRequest,
    AuthorizeResponse,
    ConnectionView,
    OAuthCallback,
    ProviderView,
    PublicationCreate,
    PublicationView,
    SandboxCode,
    SandboxConsent,
    SandboxFeed,
    SandboxPostView,
    SandboxRequestView,
)
from app.publishing.base import AuthorizationDenied, Provider, PublishError, pkce_s256
from app.publishing.compose import compose
from app.publishing.mock import issue_code

router = APIRouter(prefix="/api/v1")

STATE_TTL = timedelta(minutes=10)
PENDING = ("queued", "retrying")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _providers(request: Request) -> dict[str, Provider]:
    return request.app.state.providers


def _provider(request: Request, name: str) -> Provider:
    provider = _providers(request).get(name)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That publishing provider is not enabled")
    return provider


def _require_admin(actor: Member) -> None:
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins manage publishing accounts")


def _connection_view(request: Request, c: PublishingConnection) -> ConnectionView:
    provider = _providers(request).get(c.provider)
    return ConnectionView(
        id=c.id, provider=c.provider,
        provider_name=provider.info.display_name if provider else c.provider,
        sandbox=bool(provider and provider.info.sandbox), channel=c.channel,
        account_name=c.account_name, status=c.status, options=c.options or {},
        last_error=c.last_error, connected_by_email=c.connected_by_email,
        token_expires_at=_utc(c.token_expires_at), created_at=_utc(c.created_at),
    )


def _publication_view(request: Request, p: Publication, c: PublishingConnection) -> PublicationView:
    provider = _providers(request).get(c.provider)
    return PublicationView(
        id=p.id, scheduled_post_id=p.scheduled_post_id, connection_id=c.id,
        account_name=c.account_name, provider=c.provider, channel=c.channel,
        sandbox=bool(provider and provider.info.sandbox), status=p.status, attempts=p.attempts,
        next_attempt_at=_utc(p.next_attempt_at), error_code=p.error_code,
        last_error=p.last_error, external_id=p.external_id, external_url=p.external_url,
        published_at=_utc(p.published_at), requested_by_email=p.requested_by_email,
        created_at=_utc(p.created_at),
    )


def _connection(db: Session, actor: Member, connection_id: str) -> PublishingConnection:
    conn = db.scalar(select(PublishingConnection).where(
        PublishingConnection.id == connection_id,
        PublishingConnection.workspace_id == actor.workspace_id,
    ))
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return conn


# ── Providers and accounts ────────────────────────────────────────────────────────────────


@router.get("/publishing/providers", response_model=list[ProviderView])
def list_providers(request: Request, actor: CurrentMember):
    return [
        ProviderView(name=p.info.name, display_name=p.info.display_name, sandbox=p.info.sandbox,
                     idempotent=p.info.idempotent, channels=list(p.info.channels),
                     options={k: list(v) for k, v in p.info.options.items()})
        for p in _providers(request).values()
    ]


@router.get("/publishing/connections", response_model=list[ConnectionView])
def list_connections(request: Request, actor: CurrentMember, db: DbSession):
    rows = db.scalars(select(PublishingConnection).where(
        PublishingConnection.workspace_id == actor.workspace_id,
        PublishingConnection.status != "disconnected",
    ).order_by(PublishingConnection.created_at)).all()
    return [_connection_view(request, c) for c in rows]


@router.post("/publishing/connections/authorize", response_model=AuthorizeResponse)
def start_authorization(payload: AuthorizeRequest, request: Request, actor: CurrentMember,
                        db: DbSession):
    """Step 1 of OAuth (authorization code + PKCE). Send the admin to `authorize_url`."""
    _require_admin(actor)
    provider = _provider(request, payload.provider)
    if payload.channel not in provider.info.channels:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown channel")
    for key, value in payload.options.items():
        if value not in provider.info.options.get(key, ()):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid option {key}")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    db.add(PublishingAuthState(
        state_sha256=_sha(state), workspace_id=actor.workspace_id, user_id=actor.id,
        provider=provider.info.name, channel=payload.channel,
        code_verifier=request.app.state.cipher.seal(verifier), options=payload.options,
        expires_at=datetime.now(timezone.utc) + STATE_TTL,
    ))
    db.commit()
    url = provider.authorization_url(
        state=state, code_challenge=pkce_s256(verifier),
        redirect_uri=_redirect_uri(request), options=payload.options,
    )
    return AuthorizeResponse(authorize_url=url, state=state)


def _redirect_uri(request: Request) -> str:
    # A real provider redirects the browser here; the web app finishes with the callback.
    return f"{str(request.base_url).rstrip('/')}/#/publishing/callback"


def _pending_state(db: Session, actor: Member, state: str) -> PublishingAuthState:
    row = db.get(PublishingAuthState, _sha(state))
    if row is None or row.workspace_id != actor.workspace_id or row.user_id != actor.id:
        # Bound to the admin and workspace that started it: a stolen state is useless.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown authorization; start again")
    if row.used_at is not None or _utc(row.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_410_GONE, "This authorization expired; start again")
    return row


@router.post("/publishing/oauth/callback", response_model=ConnectionView)
def finish_authorization(payload: OAuthCallback, request: Request, actor: CurrentMember,
                         db: DbSession):
    """Step 2: trade the code for credentials and store the connection (encrypted)."""
    _require_admin(actor)
    row = _pending_state(db, actor, payload.state)
    used = db.execute(
        update(PublishingAuthState)
        .where(PublishingAuthState.state_sha256 == row.state_sha256,
               PublishingAuthState.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()  # single use, even if the exchange below fails
    if used != 1:
        raise HTTPException(status.HTTP_410_GONE, "This authorization was already used")
    if payload.error or not payload.code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Access was not granted")

    provider = _provider(request, row.provider)
    cipher = request.app.state.cipher
    try:
        grant = provider.exchange_code(
            code=payload.code, code_verifier=cipher.open(row.code_verifier),
            redirect_uri=_redirect_uri(request), options=row.options,
        )
    except AuthorizationDenied as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from None
    except PublishError as err:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The provider failed: {err}") from None

    conn = db.scalar(select(PublishingConnection).where(
        PublishingConnection.workspace_id == row.workspace_id,
        PublishingConnection.provider == row.provider,
        PublishingConnection.channel == row.channel,
        PublishingConnection.account_id == grant.account_id,
    ))
    reconnected = conn is not None
    if conn is None:
        conn = PublishingConnection(
            workspace_id=row.workspace_id, provider=row.provider, channel=row.channel,
            account_id=grant.account_id, connected_by_email=actor.email,
        )
        db.add(conn)
    conn.account_name, conn.status, conn.options = grant.account_name, "active", row.options
    conn.credentials = cipher.seal_credentials(grant.credentials)
    conn.token_expires_at, conn.last_error = grant.credentials.expires_at, None
    db.flush()
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="connection.connected",
        target_type="connection", target_id=conn.id, target_label=conn.account_name,
        after={"provider": conn.provider, "channel": conn.channel,
               "account": conn.account_name, "reconnected": reconnected},
    )
    db.commit()
    return _connection_view(request, conn)


@router.post("/publishing/sandbox/authorize", response_model=SandboxCode)
def sandbox_consent(payload: SandboxConsent, request: Request, actor: CurrentMember,
                    db: DbSession):
    """The sandbox's own authorization server (what a platform's consent screen would do).
    Real providers have no equivalent here: the admin consents on the platform's site."""
    _provider(request, "mock")
    row = _pending_state(db, actor, payload.state)
    if row.provider != "mock":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown authorization; start again")
    return SandboxCode(code=issue_code(payload.challenge) if payload.allow else "access_denied")


@router.delete("/publishing/connections/{connection_id}", response_model=ConnectionView)
def disconnect(connection_id: str, request: Request, actor: CurrentMember, db: DbSession):
    """Wipe the credentials (after a best-effort revoke) and cancel pending deliveries."""
    _require_admin(actor)
    conn = _connection(db, actor, connection_id)
    if conn.status != "disconnected":
        provider = _providers(request).get(conn.provider)
        if provider and conn.credentials:
            try:
                provider.revoke(request.app.state.cipher.open_credentials(conn.credentials),
                                account_id=conn.account_id)
            except Exception:  # noqa: BLE001 - revoke is a courtesy; wiping is what matters
                pass
        cancelled = db.execute(
            update(Publication)
            .where(Publication.connection_id == conn.id, Publication.status.in_(PENDING))
            .values(status="cancelled", error_code="connection",
                    last_error="The account was disconnected", next_attempt_at=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="connection.disconnected",
            target_type="connection", target_id=conn.id, target_label=conn.account_name,
            before={"status": conn.status},
            after={"status": "disconnected", "publications_cancelled": cancelled},
        )
        conn.status, conn.credentials, conn.token_expires_at = "disconnected", None, None
        db.commit()
    return _connection_view(request, conn)


# ── Publications ──────────────────────────────────────────────────────────────────────────


def _post_publications(db: Session, post: ScheduledPost):
    return db.execute(
        select(Publication, PublishingConnection)
        .join(PublishingConnection, PublishingConnection.id == Publication.connection_id)
        .where(Publication.scheduled_post_id == post.id)
        .order_by(Publication.created_at)
    ).all()


@router.get("/scheduled-posts/{post_id}/publications", response_model=list[PublicationView])
def list_publications(post_id: str, request: Request, actor: CurrentMember, db: DbSession):
    post = _workspace_post(db, actor, post_id)
    return [_publication_view(request, p, c) for p, c in _post_publications(db, post)]


@router.post(
    "/scheduled-posts/{post_id}/publications",
    response_model=PublicationView, status_code=status.HTTP_201_CREATED,
)
def request_publication(post_id: str, payload: PublicationCreate, request: Request,
                        actor: CurrentMember, db: DbSession):
    post = _workspace_post(db, actor, post_id)
    conn = _connection(db, actor, payload.connection_id)
    job = db.get(ApiJob, post.job_id)
    if post.status != "scheduled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Schedule the post again first")
    if job is None or job.approval_status != "approved" or job.result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only approved drafts can be published")
    if conn.status != "active" or conn.provider not in _providers(request):
        raise HTTPException(status.HTTP_409_CONFLICT, "That account needs to be reconnected")
    try:
        compose(job.result["text"], conn.channel)
    except ValueError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(err)) from None

    pub = db.scalar(select(Publication).where(
        Publication.scheduled_post_id == post.id, Publication.connection_id == conn.id))
    if pub is not None and pub.status != "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "This post already goes to that account")
    if pub is None:
        pub = Publication(workspace_id=actor.workspace_id, scheduled_post_id=post.id,
                          connection_id=conn.id, requested_by_email=actor.email)
        db.add(pub)
    pub.status, pub.error_code, pub.last_error = "queued", None, None
    pub.next_attempt_at, pub.requested_by_email = post.scheduled_at, actor.email
    db.flush()
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="publication.requested",
        target_type="publication", target_id=pub.id, target_label=conn.account_name,
        after={"account": conn.account_name, "channel": conn.channel,
               "scheduled_at": _utc(post.scheduled_at).isoformat()},
    )
    db.commit()
    return _publication_view(request, pub, conn)


def _workspace_publication(db: Session, actor: Member, publication_id: str):
    row = db.execute(
        select(Publication, PublishingConnection)
        .join(PublishingConnection, PublishingConnection.id == Publication.connection_id)
        .where(Publication.id == publication_id,
               Publication.workspace_id == actor.workspace_id)
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publication not found")
    _workspace_post(db, actor, row[0].scheduled_post_id)  # same rule as the post: owner/admin
    return row


@router.post("/publications/{publication_id}/retry", response_model=PublicationView)
def retry_publication(publication_id: str, request: Request, actor: CurrentMember,
                      db: DbSession):
    """Send a failed delivery again, or one whose outcome is unknown once someone has checked
    the account. Authorization failures need the account reconnected first."""
    pub, conn = _workspace_publication(db, actor, publication_id)
    if pub.status not in ("failed", "unknown"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed or uncertain posts are retried")
    if conn.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Reconnect the account first")
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="publication.retried",
        target_type="publication", target_id=pub.id, target_label=conn.account_name,
        before={"status": pub.status, "error_code": pub.error_code}, after={"status": "queued"},
    )
    pub.status, pub.next_attempt_at, pub.attempts = "queued", datetime.now(timezone.utc), 0
    pub.error_code = pub.last_error = None
    db.commit()
    return _publication_view(request, pub, conn)


@router.delete("/publications/{publication_id}", response_model=PublicationView)
def cancel_publication(publication_id: str, request: Request, actor: CurrentMember,
                       db: DbSession):
    pub, conn = _workspace_publication(db, actor, publication_id)
    if pub.status not in PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "It is already being sent or was sent; remove it on the platform if needed",
        )
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="publication.cancelled",
        target_type="publication", target_id=pub.id, target_label=conn.account_name,
        before={"status": pub.status}, after={"status": "cancelled"},
    )
    pub.status, pub.next_attempt_at, pub.error_code = "cancelled", None, "cancelled"
    db.commit()
    return _publication_view(request, pub, conn)


# ── Sandbox platform view ─────────────────────────────────────────────────────────────────


@router.get("/publishing/sandbox", response_model=SandboxFeed)
def sandbox_feed(request: Request, actor: CurrentMember, db: DbSession):
    """What the sandbox platform holds for this workspace's sandbox accounts: its posts and the
    requests it received (retries, 429s, duplicates) — the view a developer console gives."""
    names = dict(db.execute(select(PublishingConnection.account_id,
                                   PublishingConnection.account_name).where(
        PublishingConnection.workspace_id == actor.workspace_id,
        PublishingConnection.provider == "mock")).all())
    if not names:
        return SandboxFeed(posts=[], requests=[])
    posts = db.scalars(select(SandboxPost).where(SandboxPost.account_id.in_(names))
                       .order_by(SandboxPost.created_at.desc()).limit(50)).all()
    requests = db.scalars(select(SandboxRequest).where(SandboxRequest.account_id.in_(names))
                          .order_by(SandboxRequest.id.desc()).limit(100)).all()
    return SandboxFeed(
        posts=[SandboxPostView(external_id=p.external_id, account_name=names[p.account_id],
                               channel=p.channel, body=p.body, hashtags=p.hashtags or [],
                               state=p.state, created_at=_utc(p.created_at)) for p in posts],
        requests=[SandboxRequestView(account_name=names[r.account_id], kind=r.kind,
                                     outcome=r.outcome, created_at=_utc(r.created_at))
                  for r in requests],
    )
