"""Publishing behind a provider interface: connections, OAuth states, publications, sandbox.

Replaces the platform tables dropped in 0010 with a narrower design: credentials are
encrypted at rest, OAuth states are hashed, single-use and bound to the admin, and delivery
state lives on `publications`, driven by the worker's delivery loop. Only the sandbox provider
exists (see docs/PUBLISHING.md); `sandbox_posts` and `sandbox_requests` are its platform side.

Revision ID: 0016_publishing
Revises: 0015_draft_versions_comments
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_publishing"
down_revision = "0015_draft_versions_comments"
branch_labels = None
depends_on = None


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "publishing_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("account_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("credentials", sa.Text(), nullable=True),
        _ts("token_expires_at", nullable=True),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("connected_by_email", sa.String(320), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_publishing_connections_workspace", "publishing_connections",
                    ["workspace_id", "status"])
    op.create_table(
        "publishing_auth_states",
        sa.Column("state_sha256", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        _ts("expires_at", nullable=False),
        _ts("used_at", nullable=True),
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_post_id", sa.String(36),
                  sa.ForeignKey("scheduled_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.String(36),
                  sa.ForeignKey("publishing_connections.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at", nullable=True),
        _ts("locked_until", nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("external_url", sa.String(1024), nullable=True),
        _ts("published_at", nullable=True),
        sa.Column("requested_by_email", sa.String(320), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scheduled_post_id", "connection_id",
                            name="uq_publications_post_conn"),
    )
    op.create_index("ix_publications_due", "publications", ["status", "next_attempt_at"])
    op.create_table(
        "sandbox_posts",
        sa.Column("external_id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(128), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("hashtags", sa.JSON(), nullable=False),
        sa.Column("media_sha256", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "sandbox_requests",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(128), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sandbox_requests")
    op.drop_table("sandbox_posts")
    op.drop_index("ix_publications_due", table_name="publications")
    op.drop_table("publications")
    op.drop_table("publishing_auth_states")
    op.drop_index("ix_publishing_connections_workspace", table_name="publishing_connections")
    op.drop_table("publishing_connections")
