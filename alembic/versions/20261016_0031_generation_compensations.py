"""Explicit append-only compensation allocations.
Revision ID: 20261016_0031
Revises: 20261016_0030
"""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0031"
down_revision = "20261016_0030"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "compensation_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("generation_id", sa.String(36), sa.ForeignKey("generations.id"), nullable=False),
        sa.Column("source_ledger_entry_id", sa.String(36), sa.ForeignKey("ledger_entries.id"), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("partner_id", "idempotency_key", name="uq_compensation_partner_idem"),
        sa.CheckConstraint("amount_rub > 0", name="compensation_positive_amount"),
        sa.CheckConstraint("mode IN ('credit', 'link')", name="compensation_mode"),
    )
    for column in ("partner_id", "generation_id", "source_ledger_entry_id"):
        op.create_index("ix_compensation_allocations_" + column, "compensation_allocations", [column])
    op.execute(
        """
        CREATE FUNCTION validate_compensation_source() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE credit ledger_entries%%ROWTYPE; allocated numeric;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'compensation allocation is append-only' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM generations WHERE id = NEW.generation_id AND partner_id = NEW.partner_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'compensation generation mismatch' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO credit FROM ledger_entries WHERE id = NEW.source_ledger_entry_id FOR UPDATE;
            IF NOT FOUND OR credit.partner_id <> NEW.partner_id OR credit.amount_rub <= 0 THEN
                RAISE EXCEPTION 'invalid compensation source' USING ERRCODE = '23514';
            END IF;
            IF (NEW.mode = 'link' AND (credit.operation_type <> 'manual_adjustment'
                    OR credit.generation_id IS NOT NULL))
                OR (NEW.mode = 'credit' AND (credit.operation_type <> 'generation_refund'
                    OR credit.generation_id IS DISTINCT FROM NEW.generation_id)) THEN
                RAISE EXCEPTION 'compensation source type mismatch' USING ERRCODE = '23514';
            END IF;
            SELECT coalesce(sum(amount_rub), 0) INTO allocated FROM compensation_allocations
                WHERE source_ledger_entry_id = NEW.source_ledger_entry_id;
            IF allocated + NEW.amount_rub > credit.amount_rub THEN
                RAISE EXCEPTION 'compensation exceeds source credit' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """.replace("%%ROWTYPE", "%ROWTYPE")
    )
    op.execute("""CREATE TRIGGER validate_compensation_source BEFORE INSERT OR UPDATE ON compensation_allocations
        FOR EACH ROW EXECUTE FUNCTION validate_compensation_source()""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM compensation_allocations)")).scalar_one():
        raise RuntimeError("Compensation allocations are financial history; cannot drop nonempty table")
    op.execute("DROP TRIGGER validate_compensation_source ON compensation_allocations")
    op.execute("DROP FUNCTION validate_compensation_source()")
    op.drop_table("compensation_allocations")
