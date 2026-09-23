"""Lifetime trials, threshold history and durable provider recovery."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0020"
down_revision = "20260923_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trial_entitlements",
        sa.Column("telegram_id", sa.String(64), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
    )
    op.create_table(
        "margin_threshold_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(240), nullable=False),
        sa.Column("old_value", sa.Numeric(5, 2)),
        sa.Column("new_value", sa.Numeric(5, 2)),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_margin_threshold_history_scope", "margin_threshold_history", ["scope"])
    op.create_table(
        "provider_circuits",
        sa.Column("provider", sa.String(80), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("healthy_checks", sa.Integer(), nullable=False),
        sa.Column("real_successes", sa.Integer(), nullable=False),
        sa.Column("in_flight", sa.String(36)),
        sa.Column("probe_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_check_at", sa.DateTime(timezone=True)),
        sa.Column("recovered_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "provider_outcomes",
        sa.Column("generation_id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_outcomes_provider", "provider_outcomes", ["provider"])
    op.create_index("ix_provider_outcomes_created_at", "provider_outcomes", ["created_at"])


def downgrade():
    for table in ("provider_outcomes", "provider_circuits", "margin_threshold_history", "trial_entitlements"):
        op.drop_table(table)
