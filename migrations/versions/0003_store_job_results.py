"""Persist Gemini output on API jobs.

Revision ID: 0003_job_results
Revises: 0002_user_roles
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_job_results"
down_revision = "0002_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_jobs", sa.Column("result", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_jobs", "result")
