"""Audit explicit repair of uncertain invoice creation."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0019"
down_revision = "20260923_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_invoices", sa.Column("reconciliation_snapshot", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("payment_invoices", "reconciliation_snapshot")
