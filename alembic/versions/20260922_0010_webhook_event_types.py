"""allow multiple terminal webhook events per generation

Revision ID: 20260922_0010
Revises: 20260921_0009
Create Date: 2026-09-22
"""
import sqlalchemy as sa

from alembic import op

revision = "20260922_0010"
down_revision = "20260921_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("event_type", sa.String(length=32), nullable=True))
    op.execute(
        "UPDATE webhook_events "
        "SET event_type = COALESCE(payload->>'status', 'terminal') "
        "WHERE event_type IS NULL"
    )
    op.alter_column("webhook_events", "event_type", nullable=False)
    op.drop_constraint("uq_webhook_events_generation", "webhook_events", type_="unique")
    op.create_unique_constraint(
        "uq_webhook_events_generation_event_type",
        "webhook_events",
        ["generation_id", "event_type"],
    )
    op.create_index("ix_webhook_events_event_type", "webhook_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_webhook_events_event_type", table_name="webhook_events")
    op.drop_constraint(
        "uq_webhook_events_generation_event_type",
        "webhook_events",
        type_="unique",
    )
    op.execute(
        "DELETE FROM webhook_events newer "
        "USING webhook_events older "
        "WHERE newer.generation_id = older.generation_id "
        "AND newer.created_at > older.created_at"
    )
    op.create_unique_constraint(
        "uq_webhook_events_generation",
        "webhook_events",
        ["generation_id"],
    )
    op.drop_column("webhook_events", "event_type")
