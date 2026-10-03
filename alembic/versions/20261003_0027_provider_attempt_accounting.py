"""track per-attempt provider costs and isolate circuit outcomes

Revision ID: 20261003_0027
Revises: 20261002_0026
Create Date: 2026-10-03
"""

import sqlalchemy as sa

from alembic import op

revision = "20261003_0027"
down_revision = "20261002_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generations",
        sa.Column("provider_cost_hold_usdt", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )
    op.add_column(
        "generations",
        sa.Column("provider_cost_hold_rub", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
    )
    op.add_column("provider_attempts", sa.Column("usage_snapshot", sa.JSON(), nullable=True))
    op.add_column("provider_attempts", sa.Column("cost_status", sa.String(32), nullable=True))
    op.add_column("provider_attempts", sa.Column("provider_cost_usdt", sa.Numeric(36, 18), nullable=True))
    op.add_column("provider_attempts", sa.Column("cost_reserve_usdt", sa.Numeric(36, 18), nullable=True))
    op.create_index("ix_provider_attempts_cost_status", "provider_attempts", ["cost_status"])
    op.execute(
        sa.text(
            """
            UPDATE provider_attempts AS pa
            SET cost_status = 'unknown',
                cost_reserve_usdt = g.provider_cost_usdt_snapshot
            FROM generations AS g
            WHERE pa.generation_id = g.id
              AND pa.provider = 'argolink'
              AND pa.provider_task_id IS NOT NULL
              AND pa.status IN ('failed', 'timeout')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE generations AS g
            SET provider_cost_hold_usdt = g.provider_cost_usdt_snapshot,
                provider_cost_hold_rub = ROUND(
                    g.provider_cost_usdt_snapshot * g.rub_per_usdt_snapshot,
                    2
                )
            WHERE g.status IN ('completed', 'failed', 'cancelled')
              AND EXISTS (
                    SELECT 1
                    FROM provider_attempts AS pa
                    WHERE pa.generation_id = g.id
                      AND pa.provider = 'argolink'
                      AND pa.cost_status = 'unknown'
              )
            """
        )
    )

    op.drop_constraint("provider_outcomes_pkey", "provider_outcomes", type_="primary")
    op.create_primary_key(
        "pk_provider_outcomes_generation_provider",
        "provider_outcomes",
        ["generation_id", "provider"],
    )


def downgrade() -> None:
    # If a generation has outcomes for multiple providers, keep its earliest
    # record so the legacy single-column key can be restored deterministically.
    op.execute(
        sa.text(
            """
            DELETE FROM provider_outcomes AS duplicate
            USING provider_outcomes AS retained
            WHERE duplicate.generation_id = retained.generation_id
              AND (
                    duplicate.created_at > retained.created_at
                    OR (
                        duplicate.created_at = retained.created_at
                        AND duplicate.provider > retained.provider
                    )
                  )
            """
        )
    )
    op.drop_constraint(
        "pk_provider_outcomes_generation_provider",
        "provider_outcomes",
        type_="primary",
    )
    op.create_primary_key("provider_outcomes_pkey", "provider_outcomes", ["generation_id"])

    op.drop_index("ix_provider_attempts_cost_status", table_name="provider_attempts")
    op.drop_column("provider_attempts", "cost_reserve_usdt")
    op.drop_column("provider_attempts", "provider_cost_usdt")
    op.drop_column("provider_attempts", "cost_status")
    op.drop_column("provider_attempts", "usage_snapshot")
    op.drop_column("generations", "provider_cost_hold_rub")
    op.drop_column("generations", "provider_cost_hold_usdt")
