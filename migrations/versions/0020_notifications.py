"""Notifications: in-app messages with email copies sent by the worker (an outbox).

Revision ID: 0020_notifications
Revises: 0019_uploader_sends_for_review
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_notifications"
down_revision = "0019_uploader_sends_for_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_notifications", sa.Boolean(), nullable=False,
                                     server_default=sa.true()))
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("route", sa.String(40), nullable=True),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_status", sa.String(12), nullable=False, server_default="off"),
        sa.Column("email_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("email_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_error", sa.String(300), nullable=True),
    )
    op.create_index("ix_notifications_user", "notifications",
                    ["user_id", "workspace_id", "created_at"])
    op.create_index("ix_notifications_email_due", "notifications",
                    ["email_status", "email_next_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_email_due", table_name="notifications")
    op.drop_index("ix_notifications_user", table_name="notifications")
    op.drop_table("notifications")
    op.drop_column("users", "email_notifications")
