"""Workspaces (tenant boundary), job outbox columns, and media retention columns.

Existing data moves into one "Default workspace". Each user's old global role becomes their
role in that workspace; `users.role` keeps only the platform-level meaning (admin/super_admin).

Revision ID: 0011_workspaces_outbox
Revises: 0010_calendar_only_scheduling
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_workspaces_outbox"
down_revision = "0010_calendar_only_scheduling"
branch_labels = None
depends_on = None

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.execute(
        f"INSERT INTO workspaces (id, name) VALUES ('{DEFAULT_WORKSPACE_ID}', 'Default workspace')"
    )
    op.execute(
        f"""
        INSERT INTO workspace_members (workspace_id, user_id, role, created_at)
        SELECT '{DEFAULT_WORKSPACE_ID}', id,
               CASE role WHEN 'super_admin' THEN 'admin' WHEN 'admin' THEN 'admin'
                         WHEN 'manager' THEN 'manager' ELSE 'user' END,
               created_at
        FROM users
        """
    )
    # Workspace roles now carry "manager"; the platform role is only user/admin/super_admin.
    op.execute("UPDATE users SET role = 'user' WHERE role = 'manager'")

    for table in ("api_jobs", "scheduled_posts"):
        op.add_column(table, sa.Column("workspace_id", sa.String(36), nullable=True))
        op.execute(f"UPDATE {table} SET workspace_id = '{DEFAULT_WORKSPACE_ID}'")
        op.alter_column(table, "workspace_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_workspace_id_workspaces", table, "workspaces",
            ["workspace_id"], ["id"], ondelete="CASCADE",
        )
    op.create_index("ix_api_jobs_workspace_created", "api_jobs", ["workspace_id", "created_at"])
    op.create_index(
        "ix_scheduled_posts_workspace_at", "scheduled_posts", ["workspace_id", "scheduled_at"]
    )

    op.add_column("api_jobs", sa.Column("error", sa.String(500), nullable=True))
    op.add_column("api_jobs", sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "api_jobs", sa.Column("recoveries", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "api_jobs",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    # Everything that already exists was handed to Redis when it was created.
    op.execute("UPDATE api_jobs SET enqueued_at = created_at, updated_at = created_at")
    op.create_index("ix_api_jobs_status_updated", "api_jobs", ["status", "updated_at"])

    op.add_column(
        "media_assets",
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.add_column("media_assets", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE media_assets SET last_used_at = created_at")


def downgrade() -> None:
    op.drop_column("media_assets", "purged_at")
    op.drop_column("media_assets", "last_used_at")
    op.drop_index("ix_api_jobs_status_updated", table_name="api_jobs")
    for column in ("updated_at", "recoveries", "enqueued_at", "error"):
        op.drop_column("api_jobs", column)
    op.drop_index("ix_scheduled_posts_workspace_at", table_name="scheduled_posts")
    op.drop_index("ix_api_jobs_workspace_created", table_name="api_jobs")
    for table in ("scheduled_posts", "api_jobs"):
        op.drop_constraint(f"fk_{table}_workspace_id_workspaces", table, type_="foreignkey")
        op.drop_column(table, "workspace_id")
    # Restore the old global roles from the default workspace before dropping memberships.
    op.execute(
        f"""
        UPDATE users SET role = 'manager'
        WHERE role = 'user' AND id IN (
            SELECT user_id FROM workspace_members
            WHERE workspace_id = '{DEFAULT_WORKSPACE_ID}' AND role = 'manager'
        )
        """
    )
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
