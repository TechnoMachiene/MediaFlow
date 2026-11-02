"""Add user token-version revocation support.

Revision ID: 0008_user_token_version
Revises: 0007_super_admin
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_user_token_version"
down_revision = "0007_super_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
