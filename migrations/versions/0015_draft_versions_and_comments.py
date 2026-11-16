"""Draft editor: version history and review comments.

`draft_versions` keeps every version of a job's draft (1 = Gemini's), `draft_comments` holds
threaded review comments anchored to a version and optionally a field, and
`api_jobs.draft_version` says which version `result` currently holds.

Revision ID: 0015_draft_versions_comments
Revises: 0014_audit_events
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_draft_versions_comments"
down_revision = "0014_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_jobs", sa.Column("draft_version", sa.Integer(), server_default="1", nullable=False)
    )
    op.create_table(
        "draft_versions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32),
                  sa.ForeignKey("api_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("restored_from", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(280), nullable=True),
        sa.Column("author_id", sa.String(36), nullable=True),
        sa.Column("author_email", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("job_id", "number", name="uq_draft_versions_job_number"),
    )
    op.create_table(
        "draft_comments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(32),
                  sa.ForeignKey("api_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.String(36),
                  sa.ForeignKey("draft_comments.id", ondelete="CASCADE"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(80), nullable=True),
        sa.Column("kind", sa.String(12), nullable=False, server_default="comment"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.String(36), nullable=True),
        sa.Column("author_email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_email", sa.String(320), nullable=True),
    )
    op.create_index("ix_draft_comments_job_created", "draft_comments", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_draft_comments_job_created", table_name="draft_comments")
    op.drop_table("draft_comments")
    op.drop_table("draft_versions")
    op.drop_column("api_jobs", "draft_version")
