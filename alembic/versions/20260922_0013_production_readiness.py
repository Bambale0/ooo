"""add support tickets, profit withdrawals, admin flag

Revision ID: 20260922_0013
Revises: 20260922_0012
Create Date: 2026-09-22
"""
import sqlalchemy as sa

from alembic import op

revision = "20260922_0013"
down_revision = "20260922_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("partners", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "profit_withdrawals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(36, 18), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("correction_for_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.ForeignKeyConstraint(["correction_for_id"], ["profit_withdrawals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_profit_withdrawals_partner_id", "profit_withdrawals", ["partner_id"])

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="normal"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_tickets_partner_id", "support_tickets", ["partner_id"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])

    op.create_table(
        "support_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), nullable=False),
        sa.Column("sender_type", sa.String(length=20), nullable=False),
        sa.Column("sender_telegram_id", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("attachment_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_messages_ticket_id", "support_messages", ["ticket_id"])

    op.create_table(
        "support_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["support_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_attachments_message_id", "support_attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_support_attachments_message_id", table_name="support_attachments")
    op.drop_table("support_attachments")
    op.drop_index("ix_support_messages_ticket_id", table_name="support_messages")
    op.drop_table("support_messages")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_partner_id", table_name="support_tickets")
    op.drop_table("support_tickets")
    op.drop_index("ix_profit_withdrawals_partner_id", table_name="profit_withdrawals")
    op.drop_table("profit_withdrawals")
    op.drop_column("partners", "is_admin")