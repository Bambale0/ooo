"""Allow independent Yandex registration without a synthetic Telegram identity."""

import sqlalchemy as sa

from alembic import op

revision = "20261127_0044"
down_revision = "20261117_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("partner_applications", sa.Column("yandex_subject", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_partner_applications_yandex_subject", "partner_applications", ["yandex_subject"])
    for table in ("partners", "partner_applications", "consent_acceptances"):
        op.alter_column(table, "telegram_id", existing_type=sa.String(64), nullable=True)
    op.create_check_constraint(
        "application_login_identity", "partner_applications",
        "(telegram_id IS NOT NULL) <> (yandex_subject IS NOT NULL)",
    )


def downgrade() -> None:
    if any(op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE telegram_id IS NULL)"))
           for table in ("partners", "partner_applications", "consent_acceptances")):
        raise RuntimeError("Independent accounts exist; retain the schema and roll back application code only")
    op.drop_constraint("application_login_identity", "partner_applications", type_="check")
    for table in ("partners", "partner_applications", "consent_acceptances"):
        op.alter_column(table, "telegram_id", existing_type=sa.String(64), nullable=False)
    op.drop_constraint("uq_partner_applications_yandex_subject", "partner_applications", type_="unique")
    op.drop_column("partner_applications", "yandex_subject")
