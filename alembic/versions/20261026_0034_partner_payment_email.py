"""Remember a partner payment email.

Revision ID: 20261026_0034
Revises: 20261026_0033
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261026_0034"
down_revision: str | None = "20261026_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("partners", sa.Column("payment_email", sa.String(length=320), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE partners
            SET payment_email = (
                SELECT lower(payment_invoices.buyer_email)
                FROM payment_invoices
                WHERE payment_invoices.partner_id = partners.id
                  AND payment_invoices.buyer_email IS NOT NULL
                ORDER BY payment_invoices.created_at DESC
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1
                FROM payment_invoices
                WHERE payment_invoices.partner_id = partners.id
                  AND payment_invoices.buyer_email IS NOT NULL
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("partners", "payment_email")
