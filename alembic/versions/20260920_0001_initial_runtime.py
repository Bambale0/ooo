"""initial runtime schema

Revision ID: 20260920_0001
Revises:
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_applications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("telegram_id", sa.String(length=64), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_partner_applications_telegram_id", "partner_applications", ["telegram_id"])
    op.create_index("ix_partner_applications_status", "partner_applications", ["status"])

    op.create_table(
        "partners",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("telegram_id", sa.String(length=64), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("balance_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["partner_applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index("ix_partners_status", "partners", ["status"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_partner_id", "api_keys", ["partner_id"])

    op.create_table(
        "models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("modality", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_models_slug", "models", ["slug"])
    op.create_index("ix_models_status", "models", ["status"])

    op.create_table(
        "partner_prices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=80), nullable=False),
        sa.Column("resolution", sa.String(length=80), nullable=False),
        sa.Column("price_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("provider_cost_usdt", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "mode", "resolution", name="uq_partner_prices_variant"),
    )
    op.create_index("ix_partner_prices_model_id", "partner_prices", ["model_id"])

    op.create_table(
        "generations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("model_slug", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=80), nullable=False),
        sa.Column("resolution", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("partner_price_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("result_url", sa.Text(), nullable=True),
        sa.Column("public_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("partner_id", "idempotency_key", name="uq_generations_partner_idem"),
    )
    op.create_index("ix_generations_partner_id", "generations", ["partner_id"])
    op.create_index("ix_generations_status", "generations", ["status"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_after_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),
    )
    op.create_index("ix_ledger_entries_partner_id", "ledger_entries", ["partner_id"])
    op.create_index("ix_ledger_entries_operation_type", "ledger_entries", ["operation_type"])
    op.create_index("ix_ledger_entries_generation_id", "ledger_entries", ["generation_id"])


def downgrade() -> None:
    op.drop_table("ledger_entries")
    op.drop_table("generations")
    op.drop_table("partner_prices")
    op.drop_table("models")
    op.drop_table("api_keys")
    op.drop_table("partners")
    op.drop_table("partner_applications")
