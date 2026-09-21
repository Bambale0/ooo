"""provider poll scheduling

Revision ID: 20260921_0008
Revises: 20260921_0007
Create Date: 2026-09-21
"""
import sqlalchemy as sa

from alembic import op

revision = "20260921_0008"
down_revision = "20260921_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_attempts",
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_attempts",
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_provider_attempts_next_poll_at",
        "provider_attempts",
        ["next_poll_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_attempts_next_poll_at", table_name="provider_attempts")
    op.drop_column("provider_attempts", "poll_count")
    op.drop_column("provider_attempts", "next_poll_at")
