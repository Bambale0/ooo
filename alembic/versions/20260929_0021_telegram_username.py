"""Remember Telegram usernames for administrator partner search."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_0021"
down_revision = "20260923_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_dialogs", sa.Column("telegram_username", sa.String(32), nullable=True))
    op.create_index("ix_bot_dialogs_telegram_username", "bot_dialogs", ["telegram_username"])


def downgrade() -> None:
    op.drop_index("ix_bot_dialogs_telegram_username", table_name="bot_dialogs")
    op.drop_column("bot_dialogs", "telegram_username")
