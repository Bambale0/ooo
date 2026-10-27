"""Add Lava.top SBP payment references and webhook deduplication.

Revision ID: 20261026_0033
Revises: 20261022_0032
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261026_0033"
down_revision: str | None = "20261022_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_invoices", sa.Column("provider_reference", sa.String(length=80), nullable=True))
    op.add_column("payment_invoices", sa.Column("buyer_email", sa.String(length=320), nullable=True))
    op.create_index("ix_payment_invoices_provider_reference", "payment_invoices", ["provider_reference"])
    op.create_unique_constraint(
        "uq_payment_invoices_provider_reference",
        "payment_invoices",
        ["provider_reference"],
    )
    op.create_table(
        "lava_top_webhook_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_key", sa.String(length=180), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("provider_reference", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_lava_top_webhook_events_event_key"),
    )
    op.create_index("ix_lava_top_webhook_events_event_key", "lava_top_webhook_events", ["event_key"])
    op.create_index(
        "ix_lava_top_webhook_events_provider_reference",
        "lava_top_webhook_events",
        ["provider_reference"],
    )


def downgrade() -> None:
    op.drop_index("ix_lava_top_webhook_events_provider_reference", table_name="lava_top_webhook_events")
    op.drop_index("ix_lava_top_webhook_events_event_key", table_name="lava_top_webhook_events")
    op.drop_table("lava_top_webhook_events")
    op.drop_constraint("uq_payment_invoices_provider_reference", "payment_invoices", type_="unique")
    op.drop_index("ix_payment_invoices_provider_reference", table_name="payment_invoices")
    op.drop_column("payment_invoices", "buyer_email")
    op.drop_column("payment_invoices", "provider_reference")
