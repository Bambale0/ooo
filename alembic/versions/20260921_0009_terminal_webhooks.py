"""terminal partner webhooks

Revision ID: 20260921_0009
Revises: 20260921_0008
Create Date: 2026-09-21
"""
import sqlalchemy as sa

from alembic import op

revision = "20260921_0009"
down_revision = "20260921_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("webhook_url", sa.Text(), nullable=True))
    op.add_column("api_keys", sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True))
    op.add_column("generations", sa.Column("webhook_url_snapshot", sa.Text(), nullable=True))
    op.add_column("generations", sa.Column("webhook_secret_encrypted_snapshot", sa.Text(), nullable=True))

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["generation_id"], ["generations.id"]),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.UniqueConstraint("generation_id", name="uq_webhook_events_generation"),
    )
    op.create_index("ix_webhook_events_generation_id", "webhook_events", ["generation_id"])
    op.create_index("ix_webhook_events_partner_id", "webhook_events", ["partner_id"])
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])
    op.create_index("ix_webhook_events_next_attempt_at", "webhook_events", ["next_attempt_at"])
    op.create_index("ix_webhook_events_claimed_until", "webhook_events", ["claimed_until"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["webhook_events.id"]),
        sa.UniqueConstraint("event_id", "attempt", name="uq_webhook_delivery_attempt"),
    )
    op.create_index("ix_webhook_deliveries_event_id", "webhook_deliveries", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_event_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_events_claimed_until", table_name="webhook_events")
    op.drop_index("ix_webhook_events_next_attempt_at", table_name="webhook_events")
    op.drop_index("ix_webhook_events_status", table_name="webhook_events")
    op.drop_index("ix_webhook_events_partner_id", table_name="webhook_events")
    op.drop_index("ix_webhook_events_generation_id", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_column("generations", "webhook_secret_encrypted_snapshot")
    op.drop_column("generations", "webhook_url_snapshot")
    op.drop_column("api_keys", "webhook_secret_encrypted")
    op.drop_column("api_keys", "webhook_url")
