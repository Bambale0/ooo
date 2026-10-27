"""Index opt-in rejection fences independently of ordinary partner history."""

import sqlalchemy as sa

from alembic import op

revision = "20261027_0037"
down_revision = "20261026_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Concurrent index creation avoids blocking production generation writes.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.create_index(
                "ix_generations_rejection_partner", "generations", ["partner_id"],
                postgresql_where=sa.text(
                    "CAST((request_payload -> 'native_admission_rejection') ->> 'version' AS INTEGER) = 1"
                ),
                postgresql_concurrently=True,
            )
    else:
        op.create_index(
            "ix_generations_rejection_partner", "generations", ["partner_id"],
            sqlite_where=sa.text(
                """JSON_EXTRACT(JSON_QUOTE(JSON_EXTRACT(request_payload, '$."native_admission_rejection"')),
                '$."version"') = 1"""
            ),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index("ix_generations_rejection_partner", table_name="generations", postgresql_concurrently=True)
    else:
        op.drop_index("ix_generations_rejection_partner", table_name="generations")
