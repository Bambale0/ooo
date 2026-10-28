"""Persist the provider checkout amount for Lava top-up surcharges.

Revision ID: 20261028_0039
Revises: 20261027_0038
"""

import sqlalchemy as sa

from alembic import op

revision = "20261028_0039"
down_revision = "20261027_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_invoices", sa.Column("provider_amount_rub", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_invoices", "provider_amount_rub")
