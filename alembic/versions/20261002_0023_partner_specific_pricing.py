"""partner specific pricing

Revision ID: 20261002_0023
Revises: 20260930_0022
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_0023"
down_revision = "20260930_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable partner_id FK to partner_prices
    op.add_column(
        "partner_prices",
        sa.Column("partner_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_partner_prices_partner_id",
        "partner_prices",
        ["partner_id"],
    )
    op.create_foreign_key(
        "fk_partner_prices_partner_id",
        "partner_prices",
        "partners",
        ["partner_id"],
        ["id"],
    )

    # Add partner_id to partner_price_history for audit trail
    op.add_column(
        "partner_price_history",
        sa.Column("partner_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_partner_price_history_partner_id",
        "partner_price_history",
        ["partner_id"],
    )

    # Update unique constraint to include partner_id
    # partner_id NULL = global price, partner_id NOT NULL = partner-specific override
    op.drop_constraint("uq_partner_prices_variant", "partner_prices", type_="unique")
    op.create_unique_constraint(
        "uq_partner_prices_variant",
        "partner_prices",
        ["model_id", "mode", "resolution", "partner_id"],
    )


def downgrade() -> None:
    # Restore original unique constraint
    op.drop_constraint("uq_partner_prices_variant", "partner_prices", type_="unique")
    op.create_unique_constraint(
        "uq_partner_prices_variant",
        "partner_prices",
        ["model_id", "mode", "resolution"],
    )

    # Drop partner_price_history columns
    op.drop_index("ix_partner_price_history_partner_id", table_name="partner_price_history")
    op.drop_column("partner_price_history", "partner_id")

    # Drop partner_prices columns
    op.drop_constraint("fk_partner_prices_partner_id", "partner_prices", type_="foreignkey")
    op.drop_index("ix_partner_prices_partner_id", table_name="partner_prices")
    op.drop_column("partner_prices", "partner_id")
