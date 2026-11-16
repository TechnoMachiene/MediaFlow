"""Append-only authorization audit trail.

`audit_events` records who changed roles and memberships, invitations, draft approvals,
calendar entries and workspace settings. On PostgreSQL a trigger rejects UPDATE and DELETE, so
the application (or anyone holding its credentials) can add history but never rewrite it.

Revision ID: 0014_audit_events
Revises: 0013_workspace_invitations
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_audit_events"
down_revision = "0013_workspace_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(36),
                  sa.ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target_type", sa.String(24), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("target_label", sa.String(320), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id", "id"])
    op.create_index(
        "ix_audit_events_workspace_target", "audit_events", ["workspace_id", "target_id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'audit_events is append-only (% refused)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_index("ix_audit_events_workspace_target", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_table("audit_events")
