"""separate provider capital reserve from primary procurement snapshot

Revision ID: 20261002_0026
Revises: 20261002_0025
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_0026"
down_revision = "20261002_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generations",
        sa.Column("provider_cost_reserve_usdt", sa.Numeric(36, 18), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE generations
            SET provider_cost_reserve_usdt = provider_cost_usdt_snapshot
            WHERE provider_cost_reserve_usdt IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("generations", "provider_cost_reserve_usdt")
