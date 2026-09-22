"""Pin the upstream credential used by each job across credential rotations."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0014"
down_revision = "20260922_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_attempts") as batch:
        batch.add_column(sa.Column("credential_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_attempt_credential", "provider_credentials", ["credential_id"], ["id"])
        batch.create_index("ix_provider_attempts_credential_id", ["credential_id"])


def downgrade() -> None:
    with op.batch_alter_table("provider_attempts") as batch:
        batch.drop_index("ix_provider_attempts_credential_id")
        batch.drop_constraint("fk_attempt_credential", type_="foreignkey")
        batch.drop_column("credential_id")
