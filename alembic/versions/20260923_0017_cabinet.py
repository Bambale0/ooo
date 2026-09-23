"""Durable Telegram cabinet and notification delivery."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0017"
down_revision = "20260923_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_dialogs",
        sa.Column("telegram_id", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(80), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "bot_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_bot_actions_telegram_id", "bot_actions", ["telegram_id"])
    op.create_table(
        "bot_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("telegram_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False, unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_bot_notifications_next_attempt_at", "bot_notifications", ["next_attempt_at"])


def downgrade():
    op.drop_table("bot_notifications")
    op.drop_table("bot_actions")
    op.drop_table("bot_dialogs")
