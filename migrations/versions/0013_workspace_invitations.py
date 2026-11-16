"""Workspace invitations.

An admin or manager invites an email address to a workspace with a role. The invitee accepts
with a single-use, expiring link: a new account chooses its own password, an existing account
signs in. Only the SHA-256 of each token is stored.

Revision ID: 0013_workspace_invitations
Revises: 0012_job_idempotency_key
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_workspace_invitations"
down_revision = "0012_job_idempotency_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workspace_invitations_workspace_email", "workspace_invitations",
        ["workspace_id", "email"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_invitations_workspace_email", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
