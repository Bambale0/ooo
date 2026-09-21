"""terminal webhook core

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

    op.add_column("generations", sa.Column("api_key_id", sa.String(length=36), nullable=True))
    op.add_column("generations", sa.Column("webhook_url_snapshot", sa.Text(), nullable=True))
    op.add_column(
        "generations",
        sa.Column("webhook_secret_encrypted_snapshot", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_generations_api_key_id",
        "generations",
        "api_keys",
        ["api_key_id"],
        ["id"],
    )
    op.create_index("ix_generations_api_key_id", "generations", ["api_key_id"])

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("api_key_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("destination_url", sa.Text(), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"]),
        sa.ForeignKeyConstraint(["generation_id"], ["generations.id"]),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "generation_id",
            "event_type",
            name="uq_webhook_event_generation_type",
        ),
    )
    op.create_index("ix_webhook_events_generation_id", "webhook_events", ["generation_id"])
    op.create_index("ix_webhook_events_partner_id", "webhook_events", ["partner_id"])
    op.create_index("ix_webhook_events_api_key_id", "webhook_events", ["api_key_id"])
    op.create_index("ix_webhook_events_event_type", "webhook_events", ["event_type"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["webhook_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "attempt", name="uq_webhook_delivery_event_attempt"),
    )
    op.create_index("ix_webhook_deliveries_event_id", "webhook_deliveries", ["event_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index(
        "ix_webhook_deliveries_next_attempt_at",
        "webhook_deliveries",
        ["next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_next_attempt_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_event_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("ix_webhook_events_event_type", table_name="webhook_events")
    op.drop_index("ix_webhook_events_api_key_id", table_name="webhook_events")
    op.drop_index("ix_webhook_events_partner_id", table_name="webhook_events")
    op.drop_index("ix_webhook_events_generation_id", table_name="webhook_events")
    op.drop_table("webhook_events")

    op.drop_index("ix_generations_api_key_id", table_name="generations")
    op.drop_constraint("fk_generations_api_key_id", "generations", type_="foreignkey")
    op.drop_column("generations", "webhook_secret_encrypted_snapshot")
    op.drop_column("generations", "webhook_url_snapshot")
    op.drop_column("generations", "api_key_id")

    op.drop_column("api_keys", "webhook_secret_encrypted")
    op.drop_column("api_keys", "webhook_url")
