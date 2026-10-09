"""Real PostgreSQL serialization regressions, run against the isolated CI DB."""

import asyncio
import importlib.util
import os
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_final_client_release import seed_release

from app.accounts.models import Partner
from app.billing.client_release import release_expired_client_reserve
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import lock_generation_for_update, release_generation_reserves
from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.providers.models import ProviderAttempt


@pytest.mark.integration
async def test_downgrade_cannot_race_first_final_release(release_fixture):
    factory, _, generation_id = release_fixture
    path = Path(__file__).parents[1] / "alembic/versions/20261009_0028_final_client_reserve_release.py"
    spec = importlib.util.spec_from_file_location("final_release_downgrade_race", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    started = asyncio.Event()

    async def downgrade():
        async with factory.kw["bind"].connect() as connection:
            transaction = await connection.begin()
            try:
                started.set()

                def execute(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        migration.downgrade()

                await connection.run_sync(execute)
            finally:
                # Never alter the isolated test schema, even if the regression fails.
                await transaction.rollback()

    async with factory() as db:
        generation = await db.get(Generation, generation_id)
        assert await release_expired_client_reserve(db, generation)
        task = asyncio.create_task(downgrade())
        await started.wait()
        await asyncio.sleep(.05)
        assert not task.done()
        await db.commit()
    with pytest.raises(RuntimeError, match="after a final release"):
        await asyncio.wait_for(task, timeout=10)


@pytest.fixture
async def release_fixture():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        partner, generation, _ = await seed_release(db, enrolled=True)
        partner_id, generation_id = partner.id, generation.id
    try:
        yield factory, partner_id, generation_id
    finally:
        async with factory() as db:
            await db.execute(delete(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id))
            for model in (LedgerEntry, CoverageLedgerEntry):
                await db.execute(delete(model).where(model.partner_id == partner_id))
            await db.execute(delete(Generation).where(Generation.id == generation_id))
            await db.execute(delete(Partner).where(Partner.id == partner_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
async def test_two_final_release_workers_credit_once(release_fixture):
    factory, partner_id, generation_id = release_fixture
    both_read = asyncio.Barrier(2)

    async def release():
        async with factory() as db:
            generation = await db.get(Generation, generation_id)
            await both_read.wait()
            changed = await release_expired_client_reserve(db, generation)
            await db.commit()
            return changed

    results = await asyncio.wait_for(asyncio.gather(release(), release()), timeout=10)
    assert sorted(results) == [False, True]
    async with factory() as db:
        partner = await db.get(Partner, partner_id)
        assert partner.balance_rub == 1000
        assert partner.cost_coverage_rub == 940
        assert len(list(await db.scalars(select(LedgerEntry).where(
            LedgerEntry.generation_id == generation_id
        )))) == 2


@pytest.mark.integration
@pytest.mark.parametrize("winner", ["release", "success", "failure"])
async def test_final_release_serializes_with_terminal_settlement(release_fixture, winner):
    factory, partner_id, generation_id = release_fixture
    winning_write = asyncio.Event()
    stale_read = asyncio.Event()
    contender_started = asyncio.Event()

    async def finish(db, generation, outcome):
        generation = await lock_generation_for_update(db, generation)
        attempt = await db.scalar(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id))
        attempt.provider_task_id = "verified-terminal-task"
        attempt.status = generation.status = "completed" if outcome == "success" else "failed"
        if outcome == "success":
            await settle_actual(db, generation, {"seconds": 3})
        else:
            await release_generation_reserves(db, generation, reason="Verified terminal failure")

    async def first():
        async with factory() as db:
            generation = await db.get(Generation, generation_id)
            await lock_generation_for_update(db, generation)
            await stale_read.wait()
            if winner == "release":
                assert await release_expired_client_reserve(db, generation)
            else:
                await finish(db, generation, winner)
            winning_write.set()
            await contender_started.wait()
            await db.commit()

    async def second():
        async with factory() as db:
            # The contender deliberately carries an identity-map snapshot from
            # before the winning transaction commits, forcing lock + refresh.
            generation = await db.get(Generation, generation_id)
            stale_read.set()
            await winning_write.wait()
            contender_started.set()
            if winner == "release":
                await finish(db, generation, "success")
            else:
                assert not await release_expired_client_reserve(db, generation)
            await db.commit()

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=10)
    async with factory() as db:
        generation = await db.get(Generation, generation_id)
        partner = await db.get(Partner, partner_id)
        assert partner.balance_rub == (970 if winner == "success" else 1000)
        assert partner.cost_coverage_rub == (1000 if winner == "failure" else 970)
        assert (generation.client_reserve_released_at is not None) == (winner == "release")
        if winner != "failure":
            assert generation.actual_charge_rub == (0 if winner == "release" else 30)
            assert generation.actual_provider_cost_usdt == Decimal("1.5")
            assert generation.usage_snapshot == {"seconds": 3}
        ledger = list(await db.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation_id)))
        assert len(ledger) == 2
        assert all(item.operation_type != "generation_late_charge" for item in ledger)


@pytest.mark.integration
@pytest.mark.parametrize("operation, amount", [
    ("generation_late_charge", Decimal("-100")),
    ("generation_usage_adjustment", Decimal("-30")),
    ("generation_usage_adjustment", Decimal("70")),
    ("unrecognized_generation_adjustment", Decimal("10")),
])
async def test_database_guards_final_release_against_old_or_direct_ledger_writes(release_fixture, operation, amount):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    factory, partner_id, generation_id = release_fixture
    async with factory() as db:
        generation = await db.get(Generation, generation_id)
        assert await release_expired_client_reserve(db, generation)
        await db.commit()
    async with factory() as db:
        # Old application order: mutate cached balance, then append ledger. The
        # database rejection must roll both back, even without any new helpers.
        await db.execute(text("UPDATE partners SET balance_rub = balance_rub + :amount WHERE id = :id"), {
            "amount": amount, "id": partner_id,
        })
        with pytest.raises(IntegrityError, match="Final client release forbids"):
            await db.execute(text("""
                INSERT INTO ledger_entries
                    (id, partner_id, generation_id, operation_type, amount_rub, balance_after_rub, idempotency_key)
                VALUES (:id, :partner, :generation, :operation, :amount, :balance, :key)
            """), {
                "id": generation_id, "partner": partner_id, "generation": generation_id,
                "operation": operation, "amount": amount, "balance": Decimal("1000") + amount,
                "key": f"rolled-back-code:{generation_id}",
            })
        await db.rollback()
    async with factory() as db:
        partner = await db.get(Partner, partner_id)
        assert partner.balance_rub == 1000
        assert len(list(await db.scalars(select(LedgerEntry).where(
            LedgerEntry.generation_id == generation_id
        )))) == 2


@pytest.mark.integration
async def test_database_guard_leaves_unlinked_manual_adjustments_and_other_jobs_unchanged(release_fixture):
    from app.billing.service import apply_partner_balance_change

    factory, partner_id, generation_id = release_fixture
    async with factory() as db:
        generation = await db.get(Generation, generation_id)
        assert await release_expired_client_reserve(db, generation)
        partner = await db.get(Partner, partner_id)
        await apply_partner_balance_change(
            db, partner, Decimal("5"), "admin_adjustment", f"manual-allowed:{generation_id}",
        )
        await db.commit()
        assert partner.balance_rub == Decimal("1005")
        # The fixture itself inserted normal linked reserves; this additional
        # non-final job also proves that negative/positive adjustments still work.
        other_partner, other_generation, _ = await seed_release(db)
        try:
            await settle_actual(db, other_generation, {"seconds": 3})
            await db.commit()
            assert other_partner.balance_rub == Decimal("970")
        finally:
            for model in (ProviderAttempt, LedgerEntry, CoverageLedgerEntry):
                await db.execute(delete(model).where(model.generation_id == other_generation.id))
            await db.execute(delete(Generation).where(Generation.id == other_generation.id))
            await db.execute(delete(Partner).where(Partner.id == other_partner.id))
            await db.commit()
