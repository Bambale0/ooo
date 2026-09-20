"""accounts catalog gates and provider credentials

Revision ID: 20260920_0002
Revises: 20260920_0001
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0002"
down_revision = "20260920_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partner_applications",
        sa.Column("terms_version", sa.String(length=40), nullable=False, server_default="2026-09-19"),
    )
    op.add_column(
        "partner_applications",
        sa.Column("privacy_policy_version", sa.String(length=40), nullable=False, server_default="2026-09-19"),
    )
    op.add_column(
        "models",
        sa.Column("has_provider_integration", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "models",
        sa.Column("has_public_docs", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "models",
        sa.Column("has_successful_smoke", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "consent_acceptances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("telegram_id", sa.String(length=64), nullable=False),
        sa.Column("partner_application_id", sa.String(length=36), nullable=True),
        sa.Column("partner_id", sa.String(length=36), nullable=True),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("document_version", sa.String(length=40), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_application_id"], ["partner_applications.id"]),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_consent_acceptances_telegram_id", "consent_acceptances", ["telegram_id"])

    op.create_table(
        "partner_account_state_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_partner_account_state_history_partner_id", "partner_account_state_history", ["partner_id"])

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_provider_credentials_provider", "provider_credentials", ["provider"])

    op.create_table(
        "partner_price_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=80), nullable=False),
        sa.Column("resolution", sa.String(length=80), nullable=False),
        sa.Column("old_price_rub", sa.Numeric(18, 2), nullable=True),
        sa.Column("new_price_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("old_provider_cost_usdt", sa.Numeric(18, 6), nullable=True),
        sa.Column("new_provider_cost_usdt", sa.Numeric(18, 6), nullable=False),
        sa.Column("rub_per_usdt_snapshot", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_partner_price_history_model_id", "partner_price_history", ["model_id"])


def downgrade() -> None:
    op.drop_table("partner_price_history")
    op.drop_table("provider_credentials")
    op.drop_table("partner_account_state_history")
    op.drop_table("consent_acceptances")
    op.drop_column("models", "has_successful_smoke")
    op.drop_column("models", "has_public_docs")
    op.drop_column("models", "has_provider_integration")
    op.drop_column("partner_applications", "privacy_policy_version")
    op.drop_column("partner_applications", "terms_version")
