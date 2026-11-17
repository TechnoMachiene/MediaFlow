"""Durable stage timestamps for analytics: when a worker started a job, when it finished.

Before this, those times lived only in Redis (which forgets) and Prometheus (installation-wide).
Existing finished jobs get `completed_at = updated_at`, the best value available; their
`started_at` stays NULL, so queue-wait and processing-time analytics start from new jobs.

Revision ID: 0018_job_stage_timestamps
Revises: 0017_retention_policies
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_job_stage_timestamps"
down_revision = "0017_retention_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_jobs", sa.Column("completed_at", sa.DateTime(timezone=True),
                                        nullable=True))
    op.execute("UPDATE api_jobs SET completed_at = updated_at "
               "WHERE status IN ('done', 'failed', 'dead')")
    op.create_index("ix_api_jobs_workspace_completed", "api_jobs",
                    ["workspace_id", "completed_at"])


def downgrade() -> None:
    op.drop_index("ix_api_jobs_workspace_completed", table_name="api_jobs")
    op.drop_column("api_jobs", "completed_at")
    op.drop_column("api_jobs", "started_at")
