"""Auditable FX source chain and financial incident state."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0018"
down_revision = "20260923_0017"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("fx_rate_snapshots", "fx_fallback_settings"):
        columns = [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("rate", sa.Numeric(18, 6), nullable=name == "fx_fallback_settings"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        ]
        if name == "fx_fallback_settings":
            columns += [
                sa.Column("actor", sa.String(64), nullable=False),
                sa.Column("reason", sa.Text(), nullable=False),
            ]
        op.create_table(name, *columns)
        op.create_index(f"ix_{name}_created_at", name, ["created_at"])
    op.create_table(
        "financial_incidents",
        sa.Column("kind", sa.String(200), primary_key=True),
        sa.Column("episode", sa.String(36), nullable=False),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.Column("muted", sa.Boolean(), nullable=False),
        sa.Column("last_alert_at", sa.DateTime(timezone=True)),
        sa.Column("detail", sa.Text(), nullable=False),
    )
    op.add_column("partner_price_history", sa.Column("fx_snapshot", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("partner_price_history", "fx_snapshot")
    op.drop_table("financial_incidents")
    op.drop_table("fx_fallback_settings")
    op.drop_table("fx_rate_snapshots")
