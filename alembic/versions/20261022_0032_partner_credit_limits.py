"""Audited individual credit limits; existing partners retain the zero-credit policy."""

import sqlalchemy as sa

from alembic import op

revision = "20261022_0032"
down_revision = "20261016_0031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("partners", sa.Column("credit_limit_rub", sa.Numeric(18, 2), nullable=False, server_default="0.00"))
    op.create_check_constraint("partner_credit_limit_nonnegative", "partners", "credit_limit_rub >= 0")
    op.create_table(
        "partner_credit_limit_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("old_limit_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("new_limit_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("old_limit_rub >= 0 AND new_limit_rub >= 0", name="credit_limit_history_nonnegative"),
    )
    op.create_index("ix_partner_credit_limit_history_partner_id", "partner_credit_limit_history", ["partner_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM partner_credit_limit_history)")).scalar_one():
        raise RuntimeError("Credit limit changes are financial history; retain the schema when rolling back code")
    active_limits = sa.text("SELECT EXISTS (SELECT 1 FROM partners WHERE credit_limit_rub <> 0)")
    if op.get_bind().execute(active_limits).scalar_one():
        raise RuntimeError("Cannot remove active credit limits")
    op.drop_table("partner_credit_limit_history")
    op.drop_constraint("partner_credit_limit_nonnegative", "partners", type_="check")
    op.drop_column("partners", "credit_limit_rub")
