"""Client idempotency keys for job submission.

`POST /jobs` accepts an `Idempotency-Key` header; a retry with the same key returns the job the
first request created. The key is unique per (workspace, owner). NULLs never collide, so
existing rows and uploads without a key are unaffected.

Revision ID: 0012_job_idempotency_key
Revises: 0011_workspaces_outbox
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_job_idempotency_key"
down_revision = "0011_workspaces_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_jobs", sa.Column("idempotency_key", sa.String(255), nullable=True))
    op.create_index(
        "uq_api_jobs_idempotency",
        "api_jobs",
        ["workspace_id", "owner_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_api_jobs_idempotency", table_name="api_jobs")
    op.drop_column("api_jobs", "idempotency_key")
