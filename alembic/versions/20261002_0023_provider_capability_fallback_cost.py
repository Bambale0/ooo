"""provider capability fallback cost ceiling

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
    op.add_column(
        "provider_model_capabilities",
        sa.Column("provider_cost_ceiling_usdt", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "provider_model_capabilities",
        sa.Column("billing_unit", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_model_capabilities", "billing_unit")
    op.drop_column("provider_model_capabilities", "provider_cost_ceiling_usdt")
