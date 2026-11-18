"""Uploaders accept or rewrite a draft before it reaches review.

New finished drafts start as `with_uploader`: out of the review queue until the uploader sends
them (POST /jobs/{id}/submit). Existing rows keep their status: `pending` ones were already
in the queue.

Revision ID: 0019_uploader_sends_for_review
Revises: 0018_job_stage_timestamps
"""

from alembic import op

revision = "0019_uploader_sends_for_review"
down_revision = "0018_job_stage_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("api_jobs", "approval_status", server_default="with_uploader")


def downgrade() -> None:
    op.execute("UPDATE api_jobs SET approval_status = 'pending' "
               "WHERE approval_status = 'with_uploader'")
    op.alter_column("api_jobs", "approval_status", server_default="pending")
