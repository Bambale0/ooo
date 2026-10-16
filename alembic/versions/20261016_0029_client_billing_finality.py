"""Separate immutable free customer failures from provider accounting.

Revision ID: 20261016_0029
Revises: 20261010_0028
"""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0029"
down_revision = "20261010_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("generations", sa.Column("billing_status", sa.String(32), nullable=False, server_default="pending"))
    op.add_column("generations", sa.Column("client_finalized_at", sa.DateTime(timezone=True), nullable=True))
    # Metadata only. Never change or replay historical monetary operations.
    op.execute("UPDATE generations SET billing_status = 'charged' WHERE actual_charge_rub IS NOT NULL")
    op.create_check_constraint(
        "generation_free_failure",
        "generations",
        "billing_status != 'not_chargeable' OR (status IN ('failed', 'cancelled') "
        "AND (actual_charge_rub IS NULL OR actual_charge_rub = 0))",
    )
    op.create_check_constraint(
        "generation_waived_charge",
        "generations",
        "billing_status != 'waived' OR (status = 'completed' AND (actual_charge_rub IS NULL OR actual_charge_rub = 0))",
    )
    op.execute("""
        CREATE FUNCTION guard_closed_client_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.billing_status IN ('not_chargeable', 'waived') AND (
                NEW.billing_status IS DISTINCT FROM OLD.billing_status OR
                NEW.status IS DISTINCT FROM OLD.status OR
                NEW.actual_charge_rub IS DISTINCT FROM OLD.actual_charge_rub
            ) THEN
                RAISE EXCEPTION 'closed customer outcome is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER guard_closed_client_outcome BEFORE UPDATE ON generations
        FOR EACH ROW EXECUTE FUNCTION guard_closed_client_outcome();
    """)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM generations WHERE billing_status IN ('not_chargeable', 'waived'))")
        )
        .scalar_one()
    ):
        raise RuntimeError("Cannot remove finality protection while closed customer outcomes exist")
    op.execute("DROP TRIGGER guard_closed_client_outcome ON generations")
    op.execute("DROP FUNCTION guard_closed_client_outcome()")
    op.drop_constraint(op.f("ck_generations_generation_free_failure"), "generations", type_="check")
    op.drop_constraint(op.f("ck_generations_generation_waived_charge"), "generations", type_="check")
    op.drop_column("generations", "client_finalized_at")
    op.drop_column("generations", "billing_status")
