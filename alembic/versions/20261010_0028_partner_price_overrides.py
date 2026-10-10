"""preserve durable partner-specific prices across global pricing publications

Revision ID: 20261010_0028
Revises: 20261003_0027
Create Date: 2026-10-10
"""

import sqlalchemy as sa

from alembic import op

revision = "20261010_0028"
down_revision = "20261003_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partner_price_snapshots",
        sa.Column("is_custom", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "partner_price_override_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("partner_price_id", sa.String(length=36), nullable=False),
        sa.Column("old_price_rub", sa.Numeric(18, 2), nullable=True),
        sa.Column("new_price_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_partner_price_override_history_partner_id", "partner_price_override_history", ["partner_id"])
    op.create_index(
        "ix_partner_price_override_history_partner_price_id", "partner_price_override_history", ["partner_price_id"]
    )
    # Backfill existing explicitly negotiated rates by their difference from
    # the published global template. This is metadata only: no price or ledger
    # row is changed, and the migration contains no partner-specific hardcodes.
    op.execute(
        sa.text(
            """
            INSERT INTO partner_price_override_history (
                id, partner_id, partner_price_id, old_price_rub,
                new_price_rub, actor, reason
            )
            SELECT gen_random_uuid()::varchar(36), s.partner_id, s.partner_price_id,
                   NULL, s.price_rub, 'migration_backfill',
                   'Existing non-global partner rate preserved on migration'
            FROM partner_price_snapshots AS s
            JOIN partner_prices AS p ON p.id = s.partner_price_id
            WHERE s.price_rub <> p.price_rub
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE partner_price_snapshots AS s
            SET is_custom = TRUE
            FROM partner_prices AS p
            WHERE p.id = s.partner_price_id
              AND s.price_rub <> p.price_rub
            """
        )
    )


def downgrade() -> None:
    if op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM partner_price_snapshots WHERE is_custom)")
    ).scalar_one():
        raise RuntimeError("Cannot downgrade while custom partner prices exist: preserve or migrate overrides first")
    op.drop_index("ix_partner_price_override_history_partner_price_id", table_name="partner_price_override_history")
    op.drop_index("ix_partner_price_override_history_partner_id", table_name="partner_price_override_history")
    op.drop_table("partner_price_override_history")
    op.drop_column("partner_price_snapshots", "is_custom")
