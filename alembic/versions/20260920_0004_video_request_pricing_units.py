"""video request params and pricing units

Revision ID: 20260920_0004
Revises: 20260920_0003
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0004"
down_revision = "20260920_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partner_prices",
        sa.Column("billing_unit", sa.String(length=32), nullable=False, server_default="generation"),
    )
    op.add_column(
        "partner_price_history",
        sa.Column("billing_unit", sa.String(length=32), nullable=False, server_default="generation"),
    )
    op.add_column("generations", sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("generations", sa.Column("aspect_ratio", sa.String(length=32), nullable=True))
    op.add_column("generations", sa.Column("request_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("generations", "request_payload")
    op.drop_column("generations", "aspect_ratio")
    op.drop_column("generations", "duration_seconds")
    op.drop_column("partner_price_history", "billing_unit")
    op.drop_column("partner_prices", "billing_unit")
