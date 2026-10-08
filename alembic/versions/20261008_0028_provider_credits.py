"""append-only supplier compensation statements

Revision ID: 20261008_0028
Revises: 20261003_0027
"""

import sqlalchemy as sa

from alembic import op

revision = "20261008_0028"
down_revision = "20261003_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credits",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("reference", sa.String(160), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(36, 18), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "reference", name="uq_provider_credit_reference"),
    )
    op.create_index("ix_provider_credits_provider", "provider_credits", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_provider_credits_provider", table_name="provider_credits")
    op.drop_table("provider_credits")
