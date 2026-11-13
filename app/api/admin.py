from __future__ import annotations

import argparse
import getpass

from dotenv import load_dotenv
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.api.config import ApiSettings
from app.api.database import User, Workspace, WorkspaceMember, create_database
from app.api.roles import SUPER_ADMIN_EMAIL
from app.api.security import hash_password


def _target_workspace(db, name: str | None) -> Workspace:
    query = select(Workspace)
    if name:
        query = query.where(Workspace.name == name)
    workspace = db.scalar(query.order_by(Workspace.created_at, Workspace.id).limit(1))
    if workspace is None:
        workspace = Workspace(name=name or "Default workspace")
        db.add(workspace)
        db.flush()
    return workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or promote an administrator account")
    parser.add_argument("email", help="Email address for the admin account")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace to administer (created if missing). Default: the oldest workspace, "
        "or a new 'Default workspace' on a fresh install.",
    )
    args = parser.parse_args(argv)
    try:
        email = str(TypeAdapter(EmailStr).validate_python(args.email)).lower()
    except ValidationError:
        parser.error("email must be a valid email address")

    password = getpass.getpass("New admin password (12+ characters): ")
    if len(password) < 12:
        parser.error("password must be at least 12 characters")
    confirmation = getpass.getpass("Confirm admin password: ")
    if password != confirmation:
        parser.error("passwords do not match")

    load_dotenv()
    cfg = ApiSettings.from_env()
    engine, sessions = create_database(cfg.database_url)
    try:
        with sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            role = "super_admin" if email == SUPER_ADMIN_EMAIL else "admin"
            if user is None:
                user = User(email=email, password_hash=hash_password(password), role=role)
                db.add(user)
                action = "Created"
            else:
                user.password_hash = hash_password(password)
                user.role = role
                user.token_version += 1
                action = "Promoted and reset password for"
            db.flush()
            workspace = _target_workspace(db, args.workspace)
            member = db.get(WorkspaceMember, (workspace.id, user.id))
            if member is None:
                db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin"))
            else:
                member.role = "admin"
            workspace_name = workspace.name
        print(f"{action} {role.replace('_', ' ')} account: {email} "
              f"(admin of workspace '{workspace_name}')")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
