"""Partner-specific Lava.top surcharge exception with an immutable audit log.

Revision ID: 20261103_0042
Revises: 20261028_0041
"""
import sqlalchemy as sa

from alembic import op

revision = "20261103_0042"
down_revision = "20261028_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partners",
        sa.Column("lava_top_surcharge_exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "partner_lava_top_surcharge_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("old_exempt", sa.Boolean(), nullable=False),
        sa.Column("new_exempt", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_partner_lava_top_surcharge_history_partner_id",
        "partner_lava_top_surcharge_history",
        ["partner_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM partner_lava_top_surcharge_history)")):
        raise RuntimeError("Cannot erase partner Lava surcharge audit history")
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM partners WHERE lava_top_surcharge_exempt)")):
        raise RuntimeError("Cannot remove active Lava surcharge exemptions")
    op.drop_index("ix_partner_lava_top_surcharge_history_partner_id", table_name="partner_lava_top_surcharge_history")
    op.drop_table("partner_lava_top_surcharge_history")
    op.drop_column("partners", "lava_top_surcharge_exempt")
