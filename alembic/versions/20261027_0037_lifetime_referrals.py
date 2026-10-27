"""Lifetime referrals, commissions and separate development invoices.

Revision ID: 20261027_0037
Revises: 20261026_0036
"""

import sqlalchemy as sa

from alembic import op

revision = "20261027_0037"
down_revision = "20261026_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), nullable=False),
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.UniqueConstraint("partner_id", name="uq_referral_profiles_partner_id"),
        sa.UniqueConstraint("code", name="uq_referral_profiles_code"),
    )
    op.create_index("ix_referral_profiles_partner_id", "referral_profiles", ["partner_id"])

    op.create_table(
        "referral_attributions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("referrer_partner_id", sa.String(36), nullable=True),
        sa.Column("referred_partner_id", sa.String(36), nullable=True),
        sa.Column("referred_telegram_id", sa.String(64), nullable=False),
        sa.Column("referral_code", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["referrer_partner_id"], ["partners.id"]),
        sa.ForeignKeyConstraint(["referred_partner_id"], ["partners.id"]),
        sa.UniqueConstraint("referred_partner_id", name="uq_referral_attributions_referred_partner_id"),
        sa.UniqueConstraint("referred_telegram_id", name="uq_referral_attributions_referred_telegram_id"),
    )
    op.create_index(
        "ix_referral_attributions_referrer_partner_id",
        "referral_attributions",
        ["referrer_partner_id"],
    )
    op.create_index(
        "ix_referral_attributions_referred_partner_id",
        "referral_attributions",
        ["referred_partner_id"],
    )

    # Existing users are organic first touches. This prevents retroactive
    # attribution after rollout while keeping new deep links first-touch only.
    for table_name in ("partners", "development_requests", "bot_dialogs"):
        op.execute(
            sa.text(
                f"""
                INSERT INTO referral_attributions (id, referred_telegram_id, created_at)
                SELECT
                    substr(hash, 1, 8) || '-' || substr(hash, 9, 4) || '-' ||
                    substr(hash, 13, 4) || '-' || substr(hash, 17, 4) || '-' ||
                    substr(hash, 21, 12),
                    telegram_id,
                    now()
                FROM (
                    SELECT DISTINCT
                        telegram_id,
                        md5('referral-attribution:' || telegram_id) AS hash
                    FROM {table_name}
                ) AS known
                ON CONFLICT (referred_telegram_id) DO NOTHING
                """
            )
        )

    op.create_table(
        "referral_commissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("referrer_partner_id", sa.String(36), nullable=False),
        sa.Column("referred_telegram_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(180), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("base_amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("rate", sa.Numeric(7, 6), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("base_amount_rub > 0", name="referral_commission_positive_base"),
        sa.CheckConstraint("rate > 0 AND rate <= 1", name="referral_commission_valid_rate"),
        sa.CheckConstraint("amount_rub <> 0", name="referral_commission_nonzero_amount"),
        sa.ForeignKeyConstraint(["referrer_partner_id"], ["partners.id"]),
        sa.UniqueConstraint("event_key", name="uq_referral_commissions_event_key"),
    )
    op.create_index(
        "ix_referral_commissions_referrer_partner_id",
        "referral_commissions",
        ["referrer_partner_id"],
    )
    op.create_index(
        "ix_referral_commissions_referred_telegram_id",
        "referral_commissions",
        ["referred_telegram_id"],
    )
    op.create_index("ix_referral_commissions_kind", "referral_commissions", ["kind"])
    op.create_index("ix_referral_commissions_source_id", "referral_commissions", ["source_id"])

    op.add_column("development_requests", sa.Column("quoted_price_rub", sa.Numeric(18, 2), nullable=True))
    op.add_column("development_requests", sa.Column("buyer_email", sa.String(320), nullable=True))
    op.add_column("development_requests", sa.Column("quoted_by", sa.String(64), nullable=True))
    op.add_column("development_requests", sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "development_request_positive_quote",
        "development_requests",
        "quoted_price_rub IS NULL OR quoted_price_rub > 0",
    )
    op.create_table(
        "development_invoices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("development_request_id", sa.String(36), nullable=False),
        sa.Column("installment", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_reference", sa.String(80), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("total_price_snapshot_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("requested_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("buyer_email", sa.String(320), nullable=False),
        sa.Column("invoice_url", sa.Text(), nullable=True),
        sa.Column("creation_claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("requested_rub > 0", name="development_invoice_positive_amount"),
        sa.CheckConstraint("installment IN ('advance', 'final')", name="development_invoice_installment_kind"),
        sa.ForeignKeyConstraint(["development_request_id"], ["development_requests.id"]),
        sa.UniqueConstraint("development_request_id", "installment", name="uq_development_invoice_installment"),
        sa.UniqueConstraint("provider_reference", name="uq_development_invoice_provider_reference"),
        sa.UniqueConstraint("idempotency_key", name="uq_development_invoice_idempotency"),
    )
    op.create_index(
        "ix_development_invoices_development_request_id",
        "development_invoices",
        ["development_request_id"],
    )
    op.create_index(
        "ix_development_invoices_provider_reference",
        "development_invoices",
        ["provider_reference"],
    )
    op.create_index("ix_development_invoices_status", "development_invoices", ["status"])
    op.create_index(
        "ix_development_invoices_creation_claimed_until",
        "development_invoices",
        ["creation_claimed_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_development_invoices_creation_claimed_until", table_name="development_invoices")
    op.drop_index("ix_development_invoices_status", table_name="development_invoices")
    op.drop_index("ix_development_invoices_provider_reference", table_name="development_invoices")
    op.drop_index("ix_development_invoices_development_request_id", table_name="development_invoices")
    op.drop_table("development_invoices")
    op.drop_constraint("development_request_positive_quote", "development_requests", type_="check")
    op.drop_column("development_requests", "quoted_at")
    op.drop_column("development_requests", "quoted_by")
    op.drop_column("development_requests", "buyer_email")
    op.drop_column("development_requests", "quoted_price_rub")
    op.drop_index("ix_referral_commissions_source_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_kind", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_referred_telegram_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_referrer_partner_id", table_name="referral_commissions")
    op.drop_table("referral_commissions")
    op.drop_index("ix_referral_attributions_referred_partner_id", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_referrer_partner_id", table_name="referral_attributions")
    op.drop_table("referral_attributions")
    op.drop_index("ix_referral_profiles_partner_id", table_name="referral_profiles")
    op.drop_table("referral_profiles")
