"""Snapshot opt-in final client release policy; never enroll existing jobs.

Revision ID: 20261009_0028
Revises: 20261003_0027
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "20261009_0028"
down_revision = "20261003_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no defaults or UPDATE: previous customer jobs retain exactly
    # their accepted policy, including provisional timeout/late-charge behavior.
    op.add_column("generations", sa.Column("client_release_policy", sa.String(64), nullable=True))
    op.add_column("generations", sa.Column("client_release_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("generations", sa.Column("client_reserve_released_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_generations_client_release_due_at", "generations", ["client_release_due_at"])
    if op.get_bind().dialect.name == "postgresql":
        # Enforce finality even if an older application is rolled back while this
        # expanded schema remains deployed. Failure aborts the entire transaction,
        # including any Partner balance UPDATE issued before the ledger INSERT.
        op.execute(sa.text("""
            CREATE FUNCTION neironych_guard_final_client_release() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE final_release timestamptz;
            BEGIN
                IF NEW.generation_id IS NULL THEN
                    RETURN NEW;
                END IF;
                SELECT client_reserve_released_at INTO final_release
                FROM generations WHERE id = NEW.generation_id FOR UPDATE;
                IF final_release IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_generation_client_release_final',
                        MESSAGE = 'Final client release forbids further generation ledger entries';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER trg_generation_client_release_final
            BEFORE INSERT ON ledger_entries
            FOR EACH ROW EXECUTE FUNCTION neironych_guard_final_client_release()
        """))


def downgrade() -> None:
    # Removing a final financial fact would allow older code to recharge clients.
    # Stop rather than silently discarding a promise already applied to money.
    if op.get_context().as_sql:
        raise RuntimeError("Final client release downgrade requires an online safety check")
    if op.get_bind().dialect.name == "postgresql":
        # Serialize the eligibility check with the first final release. Otherwise
        # a release could commit between an empty check and column/trigger removal.
        op.execute(sa.text("LOCK TABLE generations IN ACCESS EXCLUSIVE MODE"))
    released = op.get_bind().execute(sa.text(
        "SELECT 1 FROM generations WHERE client_reserve_released_at IS NOT NULL LIMIT 1"
    )).scalar()
    if released is not None:
        raise RuntimeError("Cannot remove final client release policy after a final release")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER trg_generation_client_release_final ON ledger_entries"))
        op.execute(sa.text("DROP FUNCTION neironych_guard_final_client_release()"))
    op.drop_index("ix_generations_client_release_due_at", table_name="generations")
    op.drop_column("generations", "client_reserve_released_at")
    op.drop_column("generations", "client_release_due_at")
    op.drop_column("generations", "client_release_policy")
