"""Add independent manager review status to API jobs.

Revision ID: 0005_job_approval_status
Revises: 0004_api_job_cache_key
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_job_approval_status"
down_revision = "0004_api_job_cache_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_jobs",
        sa.Column(
            "approval_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_api_jobs_approval_status",
        "api_jobs",
        ["approval_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_jobs_approval_status", table_name="api_jobs")
    op.drop_column("api_jobs", "approval_status")
