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
    op.create_table(
        "registration_invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("bound_telegram_id", sa.String(64), nullable=True),
        sa.Column("used_application_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("revoked_by", sa.String(80), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'used', 'revoked')",
            name="ck_registration_invites_registration_invite_status",
        ),
        sa.ForeignKeyConstraint(["used_application_id"], ["partner_applications.id"]),
        sa.UniqueConstraint("token_hash", name="uq_registration_invites_token_hash"),
        sa.UniqueConstraint("used_application_id", name="uq_registration_invite_used_application"),
    )
    op.create_index("ix_registration_invites_status", "registration_invites", ["status"])
    op.create_index(
        "ix_registration_invites_bound_telegram_id",
        "registration_invites",
        ["bound_telegram_id"],
    )
    op.create_index(
        "ix_registration_invites_used_application_id",
        "registration_invites",
        ["used_application_id"],
    )
    op.add_column(
        "provider_credentials",
        sa.Column("registration_telegram_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_provider_credentials_registration_telegram_id",
        "provider_credentials",
        ["registration_telegram_id"],
    )
    # Preserve the current identity for existing live credentials. Deleted
    # accounts are intentionally not inferred from their original application:
    # a prior Telegram transfer cannot be reconstructed safely.
    op.execute(
        sa.text(
            """
            UPDATE provider_credentials AS credential
            SET registration_telegram_id = partner.telegram_id
            FROM partners AS partner
            WHERE credential.partner_id = partner.id
              AND partner.status <> 'deleted'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE provider_credentials AS credential
            SET registration_telegram_id = application.telegram_id
            FROM partner_applications AS application
            WHERE credential.partner_application_id = application.id
              AND credential.partner_id IS NULL
              AND credential.registration_telegram_id IS NULL
            """
        )
    )
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
    op.drop_table("registration_invites")
    op.drop_column("partners", "requires_dedicated_argolink_credential")
    op.drop_index(
        "ix_provider_credentials_registration_telegram_id",
        table_name="provider_credentials",
    )
    op.drop_column("provider_credentials", "registration_telegram_id")
