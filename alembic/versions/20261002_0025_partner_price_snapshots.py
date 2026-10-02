"""freeze retail prices for existing partners

Revision ID: 20261002_0025
Revises: 20261002_0024
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_0025"
down_revision = "20261002_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_price_snapshots",
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("partner_price_id", sa.String(length=36), nullable=False),
        sa.Column("price_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("partner_id", "partner_price_id"),
    )
    op.create_index(
        "ix_partner_price_snapshots_partner_id",
        "partner_price_snapshots",
        ["partner_id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO partner_price_snapshots (partner_id, partner_price_id, price_rub)
            SELECT p.id, pp.id, pp.price_rub
            FROM partners AS p
            CROSS JOIN partner_prices AS pp
            WHERE p.status <> 'deleted'
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_partner_price_snapshots_partner_id", table_name="partner_price_snapshots")
    op.drop_table("partner_price_snapshots")
