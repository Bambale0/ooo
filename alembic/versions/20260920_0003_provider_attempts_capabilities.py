"""provider attempts and capabilities

Revision ID: 20260920_0003
Revises: 20260920_0002
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0003"
down_revision = "20260920_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_model_capabilities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=80), nullable=False),
        sa.Column("resolution", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model_id", "mode", "resolution", name="uq_provider_capability"),
    )
    op.create_index("ix_provider_model_capabilities_provider", "provider_model_capabilities", ["provider"])
    op.create_index("ix_provider_model_capabilities_model_id", "provider_model_capabilities", ["model_id"])

    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("public_error_code", sa.String(length=80), nullable=True),
        sa.Column("raw_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["generation_id"], ["generations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id", "provider", name="uq_provider_attempt_generation_provider"),
    )
    op.create_index("ix_provider_attempts_generation_id", "provider_attempts", ["generation_id"])
    op.create_index("ix_provider_attempts_provider", "provider_attempts", ["provider"])
    op.create_index("ix_provider_attempts_provider_task_id", "provider_attempts", ["provider_task_id"])


def downgrade() -> None:
    op.drop_table("provider_attempts")
    op.drop_table("provider_model_capabilities")
