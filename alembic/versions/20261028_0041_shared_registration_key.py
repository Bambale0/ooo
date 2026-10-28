"""Shared registration credential bindings, without changing existing partners.

Revision ID: 20261028_0041
Revises: 20261028_0040
"""

import sqlalchemy as sa

from alembic import op

revision = "20261028_0041"
down_revision = "20261028_0040"
branch_labels = None
depends_on = None


CHECK_NAME = "ck_provider_key_pool_items_provider_key_pool_status"


def upgrade() -> None:
    op.create_table(
        "partner_provider_credential_bindings",
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), primary_key=True),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("provider_credentials.id"), primary_key=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_partner_provider_credential_bindings_credential_id",
        "partner_provider_credential_bindings", ["credential_id"],
    )
    with op.batch_alter_table("provider_key_pool_items") as batch:
        # Revision 0040 passed a preformatted name through the naming
        # convention again. Do the same to address its dialect-truncated name.
        batch.drop_constraint(CHECK_NAME, type_="check")
        batch.create_check_constraint(op.f(CHECK_NAME), "status IN ('available', 'assigned', 'shared')")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text(
        "SELECT EXISTS(SELECT 1 FROM partner_provider_credential_bindings) "
        "OR EXISTS(SELECT 1 FROM provider_key_pool_items WHERE status = 'shared')"
    )):
        raise RuntimeError("Shared credentials are in use; refusing a destructive downgrade")
    op.drop_table("partner_provider_credential_bindings")
    with op.batch_alter_table("provider_key_pool_items") as batch:
        batch.drop_constraint(op.f(CHECK_NAME), type_="check")
        batch.create_check_constraint(CHECK_NAME, "status IN ('available', 'assigned')")
