from decimal import Decimal

import pytest
from sqlalchemy import inspect, select

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change, release_generation_reserve
from app.generations.models import Generation


async def seed(db, *, reserve=False, suffix="one"):
    partner = Partner(
        telegram_id="comp-" + suffix, company_name="Test", project_name="Test", balance_rub=Decimal("100")
    )
    db.add(partner)
    await db.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="test-model",
        model_slug="test-model",
        mode="responses",
        resolution="default",
        prompt="",
        status="reconciliation_required" if reserve else "completed",
        billing_status="pending" if reserve else "charged",
        actual_charge_rub=None if reserve else Decimal("80"),
        partner_price_rub=Decimal("80"),
        idempotency_key="comp-gen-" + suffix,
    )
    db.add(generation)
    await db.flush()
    debit = await apply_partner_balance_change(
        db,
        partner,
        Decimal("-80"),
        "generation_reserve" if reserve else "generation_usage_charge",
        "original-comp-" + suffix,
        generation.id,
    )
    await db.commit()
    return partner, generation, debit


def endpoint(generation):
    return f"/api/v1/billing/generations/{inspect(generation).identity[0]}/compensations"


async def test_compensation_is_remaining_amount_only_and_repeats_once(client, db_session, admin_headers):
    partner, generation, debit = await seed(db_session)
    body = {"idempotency_key": "comp-request-1", "reason": "Compensate incorrect delivered result"}
    first = await client.post(endpoint(generation), headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["amount_rub"] == "80.00"
    repeated = await client.post(endpoint(generation), headers=admin_headers, json=body)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert generation.actual_charge_rub == Decimal("80")  # immutable original charge
    another = await client.post(
        endpoint(generation), headers=admin_headers, json={**body, "idempotency_key": "comp-request-2"}
    )
    assert another.status_code == 409
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 2


async def test_link_existing_manual_credit_changes_no_money_and_blocks_later_release(client, db_session, admin_headers):
    partner, generation, debit = await seed(db_session, reserve=True)
    manual = await apply_partner_balance_change(
        db_session, partner, Decimal("80"), "manual_adjustment", "original-manual-credit"
    )
    await db_session.commit()
    before = partner.balance_rub
    body = {
        "idempotency_key": "link-existing-1",
        "reason": "Link the operator credit already given to customer",
        "source_ledger_entry_id": manual.id,
    }
    first = await client.post(endpoint(generation), headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["mode"] == "link"
    await release_generation_reserve(db_session, generation, reason="later terminal cleanup")
    await db_session.commit()
    await db_session.refresh(partner)
    assert partner.balance_rub == before == Decimal("100")
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 2
    view = await client.get(endpoint(generation), headers=admin_headers)
    assert view.status_code == 200
    assert view.json()["remaining_rub"] == "0.00"
    assert view.json()["linked_manual_rub"] == "80.00"


async def test_partial_compensation_and_semantic_idempotency(client, db_session, admin_headers):
    partner, generation, _ = await seed(db_session)
    body = {
        "idempotency_key": "partial-comp-1",
        "reason": "Partial adjustment for result quality",
        "amount_rub": "30.00",
    }
    one = await client.post(endpoint(generation), headers=admin_headers, json=body)
    assert one.status_code == 200, one.text
    conflict = await client.post(endpoint(generation), headers=admin_headers, json={**body, "amount_rub": "31.00"})
    assert conflict.status_code == 409
    rest = await client.post(
        endpoint(generation),
        headers=admin_headers,
        json={"idempotency_key": "remaining-comp-2", "reason": "Compensate remaining original charge"},
    )
    assert rest.status_code == 200 and rest.json()["amount_rub"] == "50.00"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_manual_credit_cannot_be_overallocated_across_tasks(client, db_session, admin_headers):
    partner, first, _ = await seed(db_session)
    second = Generation(
        partner_id=partner.id,
        model_id="test-model",
        model_slug="test-model",
        mode="responses",
        resolution="default",
        prompt="",
        status="completed",
        billing_status="charged",
        actual_charge_rub=Decimal("80"),
        partner_price_rub=Decimal("80"),
        idempotency_key="comp-gen-second",
    )
    db_session.add(second)
    await db_session.flush()
    await apply_partner_balance_change(
        db_session, partner, Decimal("-80"), "generation_usage_charge", "second-debit", second.id
    )
    credit = await apply_partner_balance_change(
        db_session, partner, Decimal("90"), "manual_adjustment", "shared-credit"
    )
    await db_session.commit()
    body = {
        "idempotency_key": "shared-credit-first",
        "reason": "Allocate existing compensation",
        "source_ledger_entry_id": credit.id,
    }
    assert (await client.post(endpoint(first), headers=admin_headers, json=body)).status_code == 200
    too_much = await client.post(
        endpoint(second),
        headers=admin_headers,
        json={**body, "idempotency_key": "shared-credit-second", "amount_rub": "20.00"},
    )
    assert too_much.status_code == 409
    ten = await client.post(
        endpoint(second),
        headers=admin_headers,
        json={**body, "idempotency_key": "shared-credit-second", "amount_rub": "10.00"},
    )
    assert ten.status_code == 200
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("30")


async def test_cross_partner_source_credit_is_rejected(client, db_session, admin_headers):
    _, generation, _ = await seed(db_session)
    other, _, _ = await seed(db_session, suffix="other")
    credit = await apply_partner_balance_change(db_session, other, Decimal("80"), "manual_adjustment", "other-credit")
    await db_session.commit()
    result = await client.post(
        endpoint(generation),
        headers=admin_headers,
        json={
            "idempotency_key": "wrong-tenant-credit",
            "reason": "Must not cross tenant boundary",
            "source_ledger_entry_id": credit.id,
        },
    )
    assert result.status_code == 409


async def test_compensation_requires_admin_authentication(client, db_session):
    _, generation, _ = await seed(db_session)
    response = await client.post(
        endpoint(generation), json={"idempotency_key": "no-auth-credit", "reason": "Unauthorized request"}
    )
    assert response.status_code == 401


async def test_postgres_parallel_compensation_has_one_financial_effect():
    import asyncio
    import os
    from uuid import uuid4

    from fastapi import HTTPException
    from sqlalchemy import delete, func
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.billing.compensations import compensate_generation
    from app.billing.models import CompensationAllocation
    from app.billing.schemas import GenerationCompensationCreate

    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL not configured")
    engine = create_async_engine(url, pool_size=10, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        partner, generation, _ = await seed(db, suffix=str(uuid4()))
        pid, gid = partner.id, generation.id

    async def compensate(index):
        async with factory() as db:
            try:
                row = await compensate_generation(
                    db,
                    gid,
                    GenerationCompensationCreate(
                        idempotency_key=f"concurrent-compensation-{index}", reason="Concurrent full refund requests"
                    ),
                    actor="integration-test",
                )
                await db.commit()
                return row.id
            except HTTPException as error:
                await db.rollback()
                assert error.status_code == 409
                return None

    try:
        results = await asyncio.wait_for(asyncio.gather(*(compensate(i) for i in range(10))), timeout=20)
        assert len([value for value in results if value]) == 1
        async with factory() as db:
            partner = await db.get(Partner, pid)
            assert partner.balance_rub == Decimal("100")
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CompensationAllocation)
                    .where(CompensationAllocation.generation_id == gid)
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(LedgerEntry.generation_id == gid, LedgerEntry.operation_type == "generation_refund")
                )
                == 1
            )
    finally:
        async with factory() as db:
            await db.execute(delete(CompensationAllocation).where(CompensationAllocation.generation_id == gid))
            await db.execute(delete(LedgerEntry).where(LedgerEntry.generation_id == gid))
            await db.execute(delete(Generation).where(Generation.id == gid))
            await db.execute(delete(Partner).where(Partner.id == pid))
            await db.commit()
        await engine.dispose()
