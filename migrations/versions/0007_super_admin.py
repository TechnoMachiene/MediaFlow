"""Promote the designated account to protected super-admin role.

Revision ID: 0007_super_admin
Revises: 0006_media_assets
"""

from alembic import op

revision = "0007_super_admin"
down_revision = "0006_media_assets"
branch_labels = None
depends_on = None


SUPER_ADMIN_EMAIL = "abdullahtauseef988@gmail.com"


def upgrade() -> None:
    op.execute(
        "UPDATE users SET role = 'super_admin' "
        f"WHERE lower(email) = '{SUPER_ADMIN_EMAIL}'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE users SET role = 'admin' "
        f"WHERE lower(email) = '{SUPER_ADMIN_EMAIL}' AND role = 'super_admin'"
    )
