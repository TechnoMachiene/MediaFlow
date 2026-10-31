"""Store deduplication key on API jobs.

Revision ID: 0004_api_job_cache_key
Revises: 0003_job_results
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_api_job_cache_key"
down_revision = "0003_job_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_jobs", sa.Column("cache_key", sa.String(length=512), nullable=True))
    op.create_index("ix_api_jobs_cache_key", "api_jobs", ["cache_key"])


def downgrade() -> None:
    op.drop_index("ix_api_jobs_cache_key", table_name="api_jobs")
    op.drop_column("api_jobs", "cache_key")
