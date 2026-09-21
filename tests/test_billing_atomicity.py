import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change, release_generation_reserve
from app.generations.models import Generation


async def test_generation_reserve_release_is_idempotent(db_session):
    partner = Partner(
        telegram_id="reserve-release-test",
        company_name="Reserve Partner",
        project_name="Reserve Bot",
        balance_rub=Decimal("100.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="model-reserve",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="reserve-release-idem",
        partner_price_rub=Decimal("80.00"),
        prompt="reserve release",
        status="queued",
    )
    db_session.add(generation)
    await db_session.flush()

    await apply_partner_balance_change(
        db=db_session,
        partner=partner,
        amount_rub=Decimal("-80.00"),
        operation_type="generation_reserve",
        idempotency_key=f"generation-reserve:{generation.id}",
        generation_id=generation.id,
        allow_negative=False,
    )
    assert Decimal(partner.balance_rub) == Decimal("20.00")

    first_release = await release_generation_reserve(
        db_session,
        generation,
        reason="terminal generation failure",
    )
    second_release = await release_generation_reserve(
        db_session,
        generation,
        reason="duplicate terminal callback",
    )

    assert first_release is not None
    assert second_release is not None
    assert first_release.id == second_release.id
    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("100.00")

    entries_result = await db_session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.generation_id == generation.id)
        .order_by(LedgerEntry.created_at)
    )
    entries = list(entries_result.scalars().all())
    assert [entry.operation_type for entry in entries] == [
        "generation_reserve",
        "generation_reserve_release",
    ]
    assert [Decimal(entry.amount_rub) for entry in entries] == [
        Decimal("-80.00"),
        Decimal("80.00"),
    ]


async def test_generation_reserve_rejects_insufficient_balance_without_mutation(db_session):
    partner = Partner(
        telegram_id="insufficient-reserve-test",
        company_name="Insufficient Partner",
        project_name="Insufficient Bot",
        balance_rub=Decimal("10.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await apply_partner_balance_change(
            db=db_session,
            partner=partner,
            amount_rub=Decimal("-20.00"),
            operation_type="generation_reserve",
            idempotency_key=f"generation-reserve:{uuid4()}",
            allow_negative=False,
        )

    assert exc_info.value.status_code == 402
    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("10.00")

    entries_result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.partner_id == partner.id)
    )
    assert entries_result.scalars().first() is None


@pytest.mark.integration
async def test_postgres_serializes_concurrent_generation_reserves():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    partner_id: str | None = None

    try:
        async with session_factory() as setup_session:
            partner = Partner(
                telegram_id=f"concurrency-{uuid4()}",
                company_name="Concurrency Partner",
                project_name="Concurrency Bot",
                balance_rub=Decimal("100.00"),
            )
            setup_session.add(partner)
            await setup_session.commit()
            partner_id = partner.id

        ready_count = 0
        ready_lock = asyncio.Lock()
        start_gate = asyncio.Event()

        async def reserve_once(idempotency_key: str) -> int:
            nonlocal ready_count
            async with session_factory() as session:
                partner = await session.get(Partner, partner_id)
                assert partner is not None

                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        start_gate.set()

                await start_gate.wait()
                try:
                    await apply_partner_balance_change(
                        db=session,
                        partner=partner,
                        amount_rub=Decimal("-80.00"),
                        operation_type="generation_reserve",
                        idempotency_key=idempotency_key,
                        allow_negative=False,
                    )
                    await session.commit()
                    return 200
                except HTTPException as exc:
                    await session.rollback()
                    return exc.status_code

        results = await asyncio.gather(
            reserve_once(f"concurrent-reserve-a:{uuid4()}"),
            reserve_once(f"concurrent-reserve-b:{uuid4()}"),
        )
        assert sorted(results) == [200, 402]

        async with session_factory() as verify_session:
            partner = await verify_session.get(Partner, partner_id)
            assert partner is not None
            assert Decimal(partner.balance_rub) == Decimal("20.00")

            entries_result = await verify_session.execute(
                select(LedgerEntry).where(LedgerEntry.partner_id == partner_id)
            )
            entries = list(entries_result.scalars().all())
            assert len(entries) == 1
            assert Decimal(entries[0].amount_rub) == Decimal("-80.00")
    finally:
        if partner_id is not None:
            async with session_factory() as cleanup_session:
                await cleanup_session.execute(delete(LedgerEntry).where(LedgerEntry.partner_id == partner_id))
                partner = await cleanup_session.get(Partner, partner_id)
                if partner is not None:
                    await cleanup_session.delete(partner)
                await cleanup_session.commit()
        await engine.dispose()
