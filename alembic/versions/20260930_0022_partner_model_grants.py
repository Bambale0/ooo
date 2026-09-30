"""Partner grants for restricted models kept out of the public catalog."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0022"
down_revision = "20260929_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_model_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("partner_id", sa.String(36), nullable=False),
        sa.Column("granted_by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("model_id", "partner_id", name="uq_partner_model_grant"),
    )
    op.create_index("ix_partner_model_grants_model_id", "partner_model_grants", ["model_id"])
    op.create_index("ix_partner_model_grants_partner_id", "partner_model_grants", ["partner_id"])
    op.create_index("ix_partner_model_grants_revoked_at", "partner_model_grants", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_partner_model_grants_revoked_at", table_name="partner_model_grants")
    op.drop_index("ix_partner_model_grants_partner_id", table_name="partner_model_grants")
    op.drop_index("ix_partner_model_grants_model_id", table_name="partner_model_grants")
    op.drop_table("partner_model_grants")
