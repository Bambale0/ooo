"""Revocable browser cabinet sessions; no API keys stored in cookies.

Revision ID: 20261109_0043
Revises: 20261103_0042
"""

import sqlalchemy as sa

from alembic import op

revision = "20261109_0043"
down_revision = "20261103_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cabinet_web_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cabinet_web_sessions_partner_id", "cabinet_web_sessions", ["partner_id"])
    op.create_index("ix_cabinet_web_sessions_expires_at", "cabinet_web_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_cabinet_web_sessions_expires_at", table_name="cabinet_web_sessions")
    op.drop_index("ix_cabinet_web_sessions_partner_id", table_name="cabinet_web_sessions")
    op.drop_table("cabinet_web_sessions")
