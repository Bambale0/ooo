"""Durable pre-provisioned ArgoLink key pool.

Revision ID: 20261028_0040
Revises: 20261028_0039
"""

import sqlalchemy as sa

from alembic import op

revision = "20261028_0040"
down_revision = "20261028_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partners",
        sa.Column(
            "requires_dedicated_argolink_credential",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "provider_key_pool_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("added_by", sa.String(80), nullable=False),
        sa.Column("assigned_application_id", sa.String(36), nullable=True),
        sa.Column("assigned_partner_id", sa.String(36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('available', 'assigned')",
            name="ck_provider_key_pool_items_provider_key_pool_status",
        ),
        sa.ForeignKeyConstraint(["assigned_application_id"], ["partner_applications.id"]),
        sa.ForeignKeyConstraint(["assigned_partner_id"], ["partners.id"]),
        sa.UniqueConstraint("key_hash", name="uq_provider_key_pool_items_key_hash"),
        sa.UniqueConstraint("assigned_application_id", name="uq_provider_key_pool_assigned_application"),
    )
    op.create_index("ix_provider_key_pool_items_provider", "provider_key_pool_items", ["provider"])
    op.create_index("ix_provider_key_pool_items_status", "provider_key_pool_items", ["status"])
    op.create_index(
        "ix_provider_key_pool_items_assigned_application_id",
        "provider_key_pool_items",
        ["assigned_application_id"],
    )
    op.create_index(
        "ix_provider_key_pool_items_assigned_partner_id",
        "provider_key_pool_items",
        ["assigned_partner_id"],
    )


def downgrade() -> None:
    op.drop_table("provider_key_pool_items")
    op.drop_column("partners", "requires_dedicated_argolink_credential")
