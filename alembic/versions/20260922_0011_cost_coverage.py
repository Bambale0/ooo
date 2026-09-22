"""add partner cost coverage accounting

Revision ID: 20260922_0011
Revises: 20260922_0010
Create Date: 2026-09-22
"""
import sqlalchemy as sa

from alembic import op

revision = "20260922_0011"
down_revision = "20260922_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partners",
        sa.Column("cost_coverage_rub", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
    )
    op.add_column(
        "generations",
        sa.Column("provider_cost_usdt_snapshot", sa.Numeric(18, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "generations",
        sa.Column("rub_per_usdt_snapshot", sa.Numeric(18, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "generations",
        sa.Column("provider_cost_reserve_rub", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
    )

    op.create_table(
        "coverage_ledger_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("coverage_after_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_coverage_ledger_entries_idempotency_key",
        ),
    )
    op.create_index(
        "ix_coverage_ledger_entries_partner_id",
        "coverage_ledger_entries",
        ["partner_id"],
    )
    op.create_index(
        "ix_coverage_ledger_entries_operation_type",
        "coverage_ledger_entries",
        ["operation_type"],
    )
    op.create_index(
        "ix_coverage_ledger_entries_generation_id",
        "coverage_ledger_entries",
        ["generation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_coverage_ledger_entries_generation_id", table_name="coverage_ledger_entries")
    op.drop_index("ix_coverage_ledger_entries_operation_type", table_name="coverage_ledger_entries")
    op.drop_index("ix_coverage_ledger_entries_partner_id", table_name="coverage_ledger_entries")
    op.drop_table("coverage_ledger_entries")
    op.drop_column("generations", "provider_cost_reserve_rub")
    op.drop_column("generations", "rub_per_usdt_snapshot")
    op.drop_column("generations", "provider_cost_usdt_snapshot")
    op.drop_column("partners", "cost_coverage_rub")
