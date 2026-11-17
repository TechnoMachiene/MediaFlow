"""Per-workspace deletion policies and per-job deletion of originals.

Originals are shared across jobs and workspaces (content-addressed), so a job's original is
now deleted for that job (`api_jobs.media_deleted_at`) and the stored object goes once no job
still has it. Workspaces get their own policy columns (NULL = installation default).
`reviewed_at` dates review decisions, which rejected-draft retention counts from.

Revision ID: 0017_retention_policies
Revises: 0016_publishing
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_retention_policies"
down_revision = "0016_publishing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("retention_originals_days", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column(
        "retention_after_publish", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("workspaces", sa.Column("retention_rejected_days", sa.Integer(), nullable=True))
    op.add_column("api_jobs", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_jobs", sa.Column(
        "media_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_jobs", sa.Column("media_deleted_reason", sa.String(16), nullable=True))
    # Jobs whose original was already purged under the old global policy keep that state.
    op.execute(
        """
        UPDATE api_jobs SET media_deleted_at = media_assets.purged_at,
                            media_deleted_reason = 'policy'
        FROM media_assets
        WHERE media_assets.sha256 = api_jobs.media_sha256 AND media_assets.purged_at IS NOT NULL
        """
    )
    # Decisions made before this column existed: the last update is the best date we have.
    op.execute(
        "UPDATE api_jobs SET reviewed_at = updated_at WHERE approval_status <> 'pending'"
    )
    op.create_index("ix_api_jobs_media_live", "api_jobs", ["media_sha256", "media_deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_api_jobs_media_live", table_name="api_jobs")
    for column in ("media_deleted_reason", "media_deleted_at", "reviewed_at"):
        op.drop_column("api_jobs", column)
    for column in ("retention_rejected_days", "retention_after_publish",
                   "retention_originals_days"):
        op.drop_column("workspaces", column)
