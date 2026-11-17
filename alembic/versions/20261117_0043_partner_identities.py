"""Add optional Yandex login identities without changing partners or balances."""

import sqlalchemy as sa

from alembic import op

revision = "20261117_0043"
down_revision = "20261103_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "subject", name="uq_partner_identity_subject"),
        sa.UniqueConstraint("partner_id", "provider", name="uq_partner_identity_provider"),
        sa.CheckConstraint("provider = 'yandex'", name="partner_identity_provider"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM partner_identities)")):
        raise RuntimeError("Cannot erase linked login identities; disable OAuth and retain the table")
    op.drop_table("partner_identities")
