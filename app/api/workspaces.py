"""Workspace membership: invitations, leaving, renaming.

A workspace is the tenant. People join one by invitation: a manager or admin invites an email
address with a role, and the person holding the link accepts it.

* **New to MediaFlow:** they choose their own password when accepting. The inviter never knows
  it, unlike `POST /users`, where the admin sets one.
* **Already has an account** (for example in another workspace): they sign in as that email and
  accept. This is the only way an account joins a second workspace, so no tenant can pull in, or
  take over, someone else's account without that person's consent.

Tokens are `secrets.token_urlsafe(32)` (256 bits). Only their SHA-256 is stored (plus, when
email is on, a copy sealed with the publishing cipher in the email outbox, erased once the email
is sent: app/api/notifications.py), they travel in
request bodies rather than URLs (so they never reach access logs), they expire after
INVITATION_TTL_HOURS, and a guarded UPDATE makes each one single-use even under concurrent
accepts. See docs/TENANCY.md.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import audit, notifications
from app.api.database import User, Workspace, WorkspaceInvitation, WorkspaceMember
from app.api.dependencies import (
    CurrentMember,
    CurrentUser,
    DbSession,
    Member,
    OptionalUser,
    effective_role,
)
from app.api.roles import SUPER_ADMIN_EMAIL, is_admin, is_reviewer
from app.api.routes import _token
from app.api.schemas import (
    InvitationAccept,
    InvitationAccepted,
    InvitationCreate,
    InvitationCreated,
    InvitationPreview,
    InvitationToken,
    InvitationView,
    WorkspaceRename,
    WorkspaceView,
)
from app.api.security import hash_password

log = logging.getLogger("api.workspaces")
router = APIRouter(prefix="/api/v1")

INVALID_LINK = "This invitation link is not valid. Ask for a new one."
USED_LINK = "This invitation was already used, withdrawn, or has expired. Ask for a new one."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; PostgreSQL aware ones. Both are stored as UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _state(inv: WorkspaceInvitation, now: datetime | None = None) -> str:
    if inv.accepted_at is not None:
        return "accepted"
    if inv.revoked_at is not None:
        return "revoked"
    if _utc(inv.expires_at) <= (now or datetime.now(timezone.utc)):
        return "expired"
    return "pending"


def _view(inv: WorkspaceInvitation, inviter_email: str | None) -> InvitationView:
    return InvitationView(
        id=inv.id, email=inv.email, role=inv.role, status=_state(inv),
        invited_by_email=inviter_email, created_at=_utc(inv.created_at),
        expires_at=_utc(inv.expires_at),
    )


def _require_inviter(actor: Member, role: str) -> None:
    if not is_reviewer(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers and admins can invite people")
    if role == "admin" and not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can invite or manage admins")


def _account(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email))


def _by_token(db: Session, token: str, *, lock: bool = False) -> WorkspaceInvitation:
    query = select(WorkspaceInvitation).where(WorkspaceInvitation.token_sha256 == _hash(token))
    if lock:
        query = query.with_for_update()
    inv = db.scalar(query)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, INVALID_LINK)
    if _state(inv) != "pending":
        raise HTTPException(status.HTTP_410_GONE, USED_LINK)
    return inv


# ── Inviting (managers and admins of the current workspace) ───────────────────────────────


@router.post(
    "/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED
)
def create_invitation(
    payload: InvitationCreate, request: Request, actor: CurrentMember, db: DbSession
):
    """Invite an email address. Inviting the same address again withdraws the earlier link,
    so there is never more than one live link per person and workspace."""
    _require_inviter(actor, payload.role)
    existing = _account(db, payload.email)
    if existing is not None and db.get(WorkspaceMember, (actor.workspace_id, existing.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That person is already in this workspace")

    now = datetime.now(timezone.utc)
    replaced = db.execute(
        update(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.workspace_id == actor.workspace_id,
            WorkspaceInvitation.email == payload.email,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    ).rowcount
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=request.app.state.api_settings.invitation_ttl_hours)
    inv = WorkspaceInvitation(
        workspace_id=actor.workspace_id, email=payload.email, role=payload.role,
        token_sha256=_hash(token), invited_by=actor.id, created_at=now, expires_at=now + ttl,
    )
    db.add(inv)
    db.flush()
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="invitation.created",
        target_type="invitation", target_id=inv.id, target_label=inv.email,
        after={"role": inv.role, "expires_at": inv.expires_at.isoformat(),
               "replaced_links": replaced},
    )
    emailed = notifications.invitation(
        db, workspace_id=actor.workspace_id, email=inv.email, token=token, role=inv.role,
        inviter_email=actor.email, cipher=request.app.state.cipher,
    )
    db.commit()
    log.info("invitation created", extra={"invitation_id": inv.id, "role": inv.role})
    return InvitationCreated(
        **_view(inv, actor.email).model_dump(), token=token, accept_path=f"/#/invite/{token}",
        emailed=emailed,
    )


@router.get("/invitations", response_model=list[InvitationView])
def list_invitations(actor: CurrentMember, db: DbSession):
    """Pending invitations of the current workspace, newest first."""
    _require_inviter(actor, "user")
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(WorkspaceInvitation, User.email)
        .outerjoin(User, User.id == WorkspaceInvitation.invited_by)
        .where(
            WorkspaceInvitation.workspace_id == actor.workspace_id,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
            WorkspaceInvitation.expires_at > now,
        )
        .order_by(WorkspaceInvitation.created_at.desc())
        .limit(200)
    ).all()
    return [_view(inv, email) for inv, email in rows]


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation(invitation_id: str, actor: CurrentMember, db: DbSession):
    inv = db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == actor.workspace_id,
        )
    )
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
    _require_inviter(actor, inv.role)
    if inv.accepted_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This invitation was already accepted; remove the person from the team instead",
        )
    if inv.revoked_at is None:
        inv.revoked_at = datetime.now(timezone.utc)
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="invitation.revoked",
            target_type="invitation", target_id=inv.id, target_label=inv.email,
            before={"role": inv.role},
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Accepting (whoever holds the link) ────────────────────────────────────────────────────


@router.post("/invitations/lookup", response_model=InvitationPreview)
def preview_invitation(payload: InvitationToken, db: DbSession):
    """Public. Tells the invite page what to show: the workspace, and whether to ask the person
    to sign in (account exists) or to choose a password (new account)."""
    inv = _by_token(db, payload.token)
    workspace = db.get(Workspace, inv.workspace_id)
    return InvitationPreview(
        workspace_name=workspace.name, email=inv.email, role=inv.role,
        expires_at=_utc(inv.expires_at), account_exists=_account(db, inv.email) is not None,
    )


@router.post("/invitations/accept", response_model=InvitationAccepted)
def accept_invitation(
    payload: InvitationAccept, request: Request, caller: OptionalUser, db: DbSession
):
    inv = _by_token(db, payload.token, lock=True)
    if caller is not None:
        if caller.email.lower() != inv.email:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This invitation is for {inv.email}. Sign in with that account to accept it.",
            )
        user = caller
    else:
        if _account(db, inv.email) is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An account with this email already exists. Sign in to accept the invitation.",
            )
        if payload.password is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a password for your new account"
            )
        if inv.email == SUPER_ADMIN_EMAIL:
            raise HTTPException(status.HTTP_409_CONFLICT, "The super-admin account is reserved")
        user = User(email=inv.email, password_hash=hash_password(payload.password))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()  # someone created this account a moment ago
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An account with this email already exists. Sign in to accept the invitation.",
            ) from None

    # Single use, even when two accepts race: only one UPDATE can match the pending row.
    now = datetime.now(timezone.utc)
    claimed = db.execute(
        update(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.id == inv.id,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
            WorkspaceInvitation.expires_at > now,
        )
        .values(accepted_at=now, accepted_by=user.id)
        # The database decides who wins; don't re-evaluate the WHERE against the loaded row.
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise HTTPException(status.HTTP_410_GONE, USED_LINK)

    # Someone who already joined another way keeps their current role.
    membership = db.get(WorkspaceMember, (inv.workspace_id, user.id))
    if membership is None:
        membership = WorkspaceMember(workspace_id=inv.workspace_id, user_id=user.id, role=inv.role)
        db.add(membership)
        audit.record(
            db, workspace_id=inv.workspace_id, actor=user, action="member.joined",
            target_type="user", target_id=user.id, target_label=user.email,
            after={"role": inv.role, "invitation_id": inv.id, "new_account": caller is None},
        )
    db.commit()
    workspace = db.get(Workspace, inv.workspace_id)
    log.info("invitation accepted", extra={"invitation_id": inv.id, "new_account": caller is None})
    return InvitationAccepted(
        **_token(request, user).model_dump(),
        workspace=WorkspaceView(
            id=workspace.id, name=workspace.name, role=effective_role(user, membership.role)
        ),
    )


# ── The caller's own memberships ──────────────────────────────────────────────────────────


def _own_membership(db: Session, user: User, workspace_id: str) -> WorkspaceMember:
    membership = db.get(WorkspaceMember, (workspace_id, user.id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return membership


@router.post("/workspaces/{workspace_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_workspace(workspace_id: str, user: CurrentUser, db: DbSession):
    """Leave a workspace. What you made there stays with the workspace: its admins keep your
    jobs, drafts and calendar entries."""
    membership = _own_membership(db, user, workspace_id)
    others = db.scalar(
        select(func.count()).select_from(WorkspaceMember).where(
            WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id != workspace_id
        )
    )
    if not others:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This is your only workspace, so you can't leave it"
        )
    if membership.role == "admin":
        admins = db.scalar(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == "admin"
            )
        )
        if admins <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "You are this workspace's only admin. Make someone else an admin first.",
            )
    audit.record(
        db, workspace_id=workspace_id, actor=user, action="member.left",
        target_type="user", target_id=user.id, target_label=user.email,
        before={"role": membership.role},
    )
    db.delete(membership)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceView)
def rename_workspace(workspace_id: str, payload: WorkspaceRename, user: CurrentUser,
                     db: DbSession):
    membership = _own_membership(db, user, workspace_id)
    role = effective_role(user, membership.role)
    if role not in {"admin", "super_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    workspace = db.get(Workspace, workspace_id)
    if workspace.name != payload.name:
        audit.record(
            db, workspace_id=workspace_id, actor=user, action="workspace.renamed",
            target_type="workspace", target_id=workspace_id, target_label=payload.name,
            before={"name": workspace.name}, after={"name": payload.name},
        )
        workspace.name = payload.name
    db.commit()
    return WorkspaceView(id=workspace.id, name=workspace.name, role=role)
