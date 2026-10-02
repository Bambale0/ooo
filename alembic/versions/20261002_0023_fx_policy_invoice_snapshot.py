"""Invoice-scoped FX snapshots and explicit automatic/manual FX policy."""

import sqlalchemy as sa

from alembic import op

revision = "20261002_0023"
down_revision = "20260930_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_fallback_settings",
        sa.Column("automatic_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("payment_invoices", sa.Column("fx_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_invoices", "fx_snapshot")
    op.drop_column("fx_fallback_settings", "automatic_enabled")
