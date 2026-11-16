from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.database import User, Workspace, WorkspaceMember
from app.api.roles import is_super_admin
from app.api.security import decode_versioned_access_token
from app.storage import MediaStorage
from app.wiring import Runtime

bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    with request.app.state.session_factory() as session:
        yield session


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def get_storage(request: Request) -> MediaStorage:
    return request.app.state.storage


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_versioned_access_token(
        credentials.credentials,
        request.app.state.api_settings.jwt_secret,
    )
    user = db.get(User, claims[0]) if claims else None
    if user is None or user.token_version != claims[1]:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user_id = user.id  # for the access log (ids only, never emails)
    return user


def optional_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    """The caller if they sent a token (which must then be valid), otherwise None."""
    if credentials is None:
        return None
    return current_user(request, credentials, db)


@dataclass(frozen=True)
class Member:
    """The caller acting inside one workspace. `role` is their role *in that workspace*
    ("super_admin" for the protected platform owner), so role checks are tenant-local."""

    user: User
    workspace_id: str
    workspace_name: str
    role: str

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def created_at(self) -> datetime:
        return self.user.created_at


def effective_role(user: User, membership_role: str) -> str:
    return "super_admin" if is_super_admin(user) else membership_role


def current_member(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id", max_length=36)] = None,
) -> Member:
    """Resolve the workspace for this request: the X-Workspace-Id header if sent, otherwise
    the caller's oldest membership. Every tenant-scoped query filters on the result."""
    query = (
        select(WorkspaceMember.role, Workspace.id, Workspace.name)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user.id)
    )
    if workspace_id:
        query = query.where(WorkspaceMember.workspace_id == workspace_id)
    else:
        query = query.order_by(WorkspaceMember.created_at, WorkspaceMember.workspace_id).limit(1)
    row = db.execute(query).first()
    if row is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You are not a member of this workspace"
            if workspace_id
            else "Your account does not belong to any workspace",
        )
    role, ws_id, ws_name = row
    request.state.workspace_id = ws_id
    return Member(user=user, workspace_id=ws_id, workspace_name=ws_name,
                  role=effective_role(user, role))


DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]
CurrentMember = Annotated[Member, Depends(current_member)]
QueueRuntime = Annotated[Runtime, Depends(get_runtime)]
Storage = Annotated[MediaStorage, Depends(get_storage)]
