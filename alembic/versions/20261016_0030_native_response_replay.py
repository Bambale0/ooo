"""Encrypted bounded native response replay; idempotency tombstones remain.
Revision ID: 20261016_0030
Revises: 20261016_0029
"""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0030"
down_revision = "20261016_0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_responses",
        sa.Column("generation_id", sa.String(36), sa.ForeignKey("generations.id"), primary_key=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(80), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("encrypted_body", sa.Text(), nullable=False),
        sa.Column("body_size", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_native_responses_expires_at", "native_responses", ["expires_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM native_responses)")).scalar_one():
        raise RuntimeError("Expire retained native responses before removing replay storage")
    op.drop_index("ix_native_responses_expires_at", table_name="native_responses")
    op.drop_table("native_responses")
