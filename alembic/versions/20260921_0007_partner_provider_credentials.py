"""per-partner encrypted provider credentials

Revision ID: 20260921_0007
Revises: 20260920_0006
Create Date: 2026-09-21
"""
import sqlalchemy as sa

from alembic import op

revision = "20260921_0007"
down_revision = "20260920_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_credentials", sa.Column("encrypted_api_key", sa.Text(), nullable=True))
    op.add_column(
        "provider_credentials",
        sa.Column("partner_application_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "provider_credentials",
        sa.Column("partner_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_provider_credentials_partner_application_id",
        "provider_credentials",
        "partner_applications",
        ["partner_application_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_provider_credentials_partner_id",
        "provider_credentials",
        "partners",
        ["partner_id"],
        ["id"],
    )
    op.create_index(
        "ix_provider_credentials_partner_application_id",
        "provider_credentials",
        ["partner_application_id"],
    )
    op.create_index("ix_provider_credentials_partner_id", "provider_credentials", ["partner_id"])
    op.execute("UPDATE provider_credentials SET is_active = false")


def downgrade() -> None:
    op.drop_index("ix_provider_credentials_partner_id", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_partner_application_id", table_name="provider_credentials")
    op.drop_constraint(
        "fk_provider_credentials_partner_id",
        "provider_credentials",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_provider_credentials_partner_application_id",
        "provider_credentials",
        type_="foreignkey",
    )
    op.drop_column("provider_credentials", "partner_id")
    op.drop_column("provider_credentials", "partner_application_id")
    op.drop_column("provider_credentials", "encrypted_api_key")
