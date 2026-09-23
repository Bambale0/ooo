"""Auditable wallet snapshots and idempotent treasury records."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0016"
down_revision = "20260923_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_invoices", sa.Column("coverage_snapshot", sa.JSON(), nullable=True))
    op.create_table(
        "wallet_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("available_usdt", sa.Numeric(36, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_snapshots_created_at", "wallet_snapshots", ["created_at"])
    op.add_column("profit_withdrawals", sa.Column("idempotency_key", sa.String(160), nullable=True))
    op.add_column("profit_withdrawals", sa.Column("override_reason", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_profit_withdrawals_idempotency_key", "profit_withdrawals", ["idempotency_key"])


def downgrade():
    op.drop_column("payment_invoices", "coverage_snapshot")
    op.drop_constraint("uq_profit_withdrawals_idempotency_key", "profit_withdrawals", type_="unique")
    op.drop_column("profit_withdrawals", "override_reason")
    op.drop_column("profit_withdrawals", "idempotency_key")
    op.drop_table("wallet_snapshots")
