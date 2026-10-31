"""Add user, manager, and admin roles.

Revision ID: 0002_user_roles
Revises: 0001_api_tables
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_user_roles"
down_revision = "0001_api_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(length=16), server_default="user", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "role")
