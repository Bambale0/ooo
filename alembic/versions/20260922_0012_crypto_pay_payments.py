"""add Crypto Pay payment accounting

Revision ID: 20260922_0012
Revises: 20260922_0011
Create Date: 2026-09-22
"""
import sqlalchemy as sa

from alembic import op

revision = "20260922_0012"
down_revision = "20260922_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_invoices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("provider_invoice_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("requested_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("accepted_assets", sa.String(length=120), nullable=False),
        sa.Column("invoice_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creation_claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_asset", sa.String(length=20), nullable=True),
        sa.Column("paid_amount", sa.Numeric(36, 18), nullable=True),
        sa.Column("paid_fiat_rate", sa.Numeric(36, 18), nullable=True),
        sa.Column("paid_usd_rate", sa.Numeric(36, 18), nullable=True),
        sa.Column("was_expired_when_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_rub", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("partner_id", "idempotency_key", name="uq_payment_invoice_partner_idempotency"),
        sa.UniqueConstraint("provider_invoice_id", name="uq_payment_invoice_provider_invoice"),
    )
    op.create_index("ix_payment_invoices_partner_id", "payment_invoices", ["partner_id"])
    op.create_index("ix_payment_invoices_status", "payment_invoices", ["status"])
    op.create_index("ix_payment_invoices_expires_at", "payment_invoices", ["expires_at"])
    op.create_index(
        "ix_payment_invoices_creation_claimed_until",
        "payment_invoices",
        ["creation_claimed_until"],
    )

    op.create_table(
        "crypto_pay_webhook_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("update_type", sa.String(length=80), nullable=False),
        sa.Column("provider_invoice_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("update_id"),
    )
    op.create_index("ix_crypto_pay_webhook_events_update_id", "crypto_pay_webhook_events", ["update_id"])
    op.create_index(
        "ix_crypto_pay_webhook_events_provider_invoice_id",
        "crypto_pay_webhook_events",
        ["provider_invoice_id"],
    )

    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("payment_invoice_id", sa.String(length=36), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["payment_invoice_id"], ["payment_invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_refund_idempotency"),
    )
    op.create_index("ix_payment_refunds_payment_invoice_id", "payment_refunds", ["payment_invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_refunds_payment_invoice_id", table_name="payment_refunds")
    op.drop_table("payment_refunds")
    op.drop_index(
        "ix_crypto_pay_webhook_events_provider_invoice_id",
        table_name="crypto_pay_webhook_events",
    )
    op.drop_index("ix_crypto_pay_webhook_events_update_id", table_name="crypto_pay_webhook_events")
    op.drop_table("crypto_pay_webhook_events")
    op.drop_index("ix_payment_invoices_creation_claimed_until", table_name="payment_invoices")
    op.drop_index("ix_payment_invoices_expires_at", table_name="payment_invoices")
    op.drop_index("ix_payment_invoices_status", table_name="payment_invoices")
    op.drop_index("ix_payment_invoices_partner_id", table_name="payment_invoices")
    op.drop_table("payment_invoices")
