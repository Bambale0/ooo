"""retry state for provider attempts and media assets

Revision ID: 20260920_0006
Revises: 20260920_0005
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0006"
down_revision = "20260920_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_attempts",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("provider_attempts", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("provider_attempts", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index("ix_provider_attempts_next_attempt_at", "provider_attempts", ["next_attempt_at"])

    op.add_column(
        "media_assets",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("media_assets", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("media_assets", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index("ix_media_assets_next_attempt_at", "media_assets", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_media_assets_next_attempt_at", table_name="media_assets")
    op.drop_column("media_assets", "last_error")
    op.drop_column("media_assets", "next_attempt_at")
    op.drop_column("media_assets", "retry_count")
    op.drop_index("ix_provider_attempts_next_attempt_at", table_name="provider_attempts")
    op.drop_column("provider_attempts", "last_error")
    op.drop_column("provider_attempts", "next_attempt_at")
    op.drop_column("provider_attempts", "retry_count")
