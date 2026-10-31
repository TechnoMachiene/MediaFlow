"""Store uploaded media once by content and retain per-job original names.

Revision ID: 0006_media_assets
Revises: 0005_job_approval_status
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_media_assets"
down_revision = "0005_job_approval_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("stored_filename", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.add_column("api_jobs", sa.Column("media_sha256", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_api_jobs_media_sha256_media_assets",
        "api_jobs",
        "media_assets",
        ["media_sha256"],
        ["sha256"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_api_jobs_media_sha256", "api_jobs", ["media_sha256"])


def downgrade() -> None:
    op.drop_index("ix_api_jobs_media_sha256", table_name="api_jobs")
    op.drop_constraint("fk_api_jobs_media_sha256_media_assets", "api_jobs", type_="foreignkey")
    op.drop_column("api_jobs", "media_sha256")
    op.drop_table("media_assets")
