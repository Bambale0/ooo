"""Record actual usage separately from immutable reservation snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0015"
down_revision = "20260923_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "generations", "provider_cost_usdt_snapshot", type_=sa.Numeric(36, 18), existing_type=sa.Numeric(18, 6)
    )
    op.add_column("generations", sa.Column("actual_charge_rub", sa.Numeric(18, 2), nullable=True))
    op.add_column("generations", sa.Column("actual_provider_cost_usdt", sa.Numeric(36, 18), nullable=True))
    op.add_column("generations", sa.Column("usage_snapshot", sa.JSON(), nullable=True))


def downgrade():
    op.alter_column(
        "generations", "provider_cost_usdt_snapshot", type_=sa.Numeric(18, 6), existing_type=sa.Numeric(36, 18)
    )
    op.drop_column("generations", "usage_snapshot")
    op.drop_column("generations", "actual_provider_cost_usdt")
    op.drop_column("generations", "actual_charge_rub")
