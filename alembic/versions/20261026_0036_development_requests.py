"""Persist independent Telegram development requests.

Revision ID: 20261026_0036
Revises: 20261026_0035
"""

import sqlalchemy as sa

from alembic import op

revision = "20261026_0036"
down_revision = "20261026_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "development_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_id", sa.String(64), nullable=False),
        sa.Column("telegram_username", sa.String(32), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_development_requests_telegram_id", "development_requests", ["telegram_id"])
    op.create_index("ix_development_requests_status", "development_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_development_requests_status", table_name="development_requests")
    op.drop_index("ix_development_requests_telegram_id", table_name="development_requests")
    op.drop_table("development_requests")
