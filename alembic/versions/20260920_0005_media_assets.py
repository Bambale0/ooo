"""media assets for partner-facing result urls

Revision ID: 20260920_0005
Revises: 20260920_0004
Create Date: 2026-09-20
"""
import sqlalchemy as sa

from alembic import op

revision = "20260920_0005"
down_revision = "20260920_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("partner_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_content_url", sa.Text(), nullable=False),
        sa.Column("storage_backend", sa.String(length=80), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("public_url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["generation_id"], ["generations.id"]),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id", name="uq_media_assets_generation"),
    )
    op.create_index("ix_media_assets_generation_id", "media_assets", ["generation_id"])
    op.create_index("ix_media_assets_partner_id", "media_assets", ["partner_id"])
    op.create_index("ix_media_assets_status", "media_assets", ["status"])


def downgrade() -> None:
    op.drop_table("media_assets")
