"""Add OAuth accounts, scheduled posts, and per-account publishing queue.

Revision ID: 0009_scheduled_publishing
Revises: 0008_user_token_version
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_scheduled_publishing"
down_revision = "0008_user_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_accounts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("account_name", sa.String(255), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "external_id", name="uq_platform_accounts_platform_external"
        ),
    )
    op.create_index(
        "ix_platform_accounts_platform_status", "platform_accounts", ["platform", "status"]
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("initiated_by", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_table(
        "scheduled_posts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("content_overrides", sa.JSON(), nullable=False),
        sa.Column("tiktok_privacy_level", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["api_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_scheduled_posts_job"),
    )
    op.create_index(
        "ix_scheduled_posts_owner_at", "scheduled_posts", ["owner_id", "scheduled_at"]
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("scheduled_post_id", sa.String(36), nullable=False),
        sa.Column("platform_account_id", sa.String(36), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["scheduled_post_id"], ["scheduled_posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["platform_account_id"], ["platform_accounts.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scheduled_post_id", "platform_account_id",
                            name="uq_publications_target"),
    )
    op.create_index(
        "ix_publications_queue",
        "publications",
        ["platform_account_id", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_publications_queue", table_name="publications")
    op.drop_table("publications")
    op.drop_index("ix_scheduled_posts_owner_at", table_name="scheduled_posts")
    op.drop_table("scheduled_posts")
    op.drop_table("oauth_states")
    op.drop_index("ix_platform_accounts_platform_status", table_name="platform_accounts")
    op.drop_table("platform_accounts")
