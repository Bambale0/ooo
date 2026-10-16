"""Real PostgreSQL: finality, trigger enforcement and concurrent settlement."""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import apply_cost_coverage_change
from app.generations.models import Generation, NativeResponse
from app.inference.replay import replay_response
from app.inference.router import fail, finish
from app.providers.models import ProviderAttempt, ProviderOutcome
from app.webhooks.models import WebhookEvent


async def seed(factory):
    async with factory() as db:
        partner = Partner(
            telegram_id="native-pg-" + str(uuid4()),
            company_name="Isolated Test",
            project_name="Isolated Test",
            balance_rub=Decimal("100"),
            cost_coverage_rub=Decimal("100"),
        )
        db.add(partner)
        await db.flush()
        generation = Generation(
            partner_id=partner.id,
            model_id="isolated-native-test",
            model_slug="isolated-native-test",
            mode="responses",
            resolution="default",
            status="submitting",
            prompt="",
            idempotency_key=str(uuid4()),
            partner_price_rub=Decimal("10"),
            provider_cost_usdt_snapshot=Decimal(".1"),
            provider_cost_reserve_usdt=Decimal(".1"),
            provider_cost_reserve_rub=Decimal("10"),
            provider_cost_hold_usdt=Decimal("0"),
            provider_cost_hold_rub=Decimal("0"),
            rub_per_usdt_snapshot=Decimal("100"),
            webhook_url_snapshot="https://partner.example/terminal",
            request_payload={
                "protocol": "responses",
                "partner_billing_policy": "actual_only",
                "rates": {
                    "input_tokens": {"retail": "1000", "cost": "1"},
                    "output_tokens": {"retail": "1000", "cost": "1"},
                },
            },
        )
        db.add(generation)
        await db.flush()
        attempt = ProviderAttempt(generation_id=generation.id, provider="argolink", status="submitting")
        db.add(attempt)
        await db.flush()
        await apply_cost_coverage_change(
            db,
            partner,
            Decimal("-10"),
            "provider_cost_reserve",
            "provider-cost-reserve:" + generation.id,
            generation.id,
        )
        await db.commit()
        return partner.id, generation.id, attempt.id


async def cleanup(factory, ids):
    partner_id, generation_id, _ = ids
    async with factory() as db:
        for model in (NativeResponse, WebhookEvent, ProviderOutcome, ProviderAttempt, LedgerEntry, CoverageLedgerEntry):
            await db.execute(delete(model).where(model.generation_id == generation_id))
        await db.execute(delete(Generation).where(Generation.id == generation_id))
        await db.execute(delete(Partner).where(Partner.id == partner_id))
        await db.commit()


def infrastructure():
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(url, pool_size=10, max_overflow=0)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.integration
async def test_postgres_closed_failure_rejects_late_finishes_and_direct_reopen():
    engine, factory = infrastructure()
    ids = await seed(factory)
    try:
        async with factory() as db:
            g, a = await db.get(Generation, ids[1]), await db.get(ProviderAttempt, ids[2])
            await fail(db, g, a, "provider_temporarily_unavailable", definitive=False)

        async def late():
            async with factory() as db:
                g, a = await db.get(Generation, ids[1]), await db.get(ProviderAttempt, ids[2])
                assert not await finish(db, g, a, {"input_tokens": 20, "output_tokens": 30})

        await asyncio.wait_for(asyncio.gather(*(late() for _ in range(10))), timeout=20)
        async with factory() as db:
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        text("UPDATE generations SET billing_status='pending' WHERE id=:id"), {"id": ids[1]}
                    )
            g = await db.get(Generation, ids[1])
            p = await db.get(Partner, ids[0])
            assert g.status == "failed" and g.actual_charge_rub is None
            assert p.balance_rub == Decimal("100")
            assert (
                await db.scalar(
                    select(func.count()).select_from(LedgerEntry).where(LedgerEntry.generation_id == ids[1])
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(func.count()).select_from(WebhookEvent).where(WebhookEvent.generation_id == ids[1])
                )
                == 1
            )
    finally:
        await cleanup(factory, ids)
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_ten_finishes_have_one_charge_one_outbox_and_durable_replay():
    engine, factory = infrastructure()
    ids = await seed(factory)
    result = {"id": ids[1], "output": [{"content": "recoverable answer"}]}
    try:

        async def complete():
            async with factory() as db:
                g, a = await db.get(Generation, ids[1]), await db.get(ProviderAttempt, ids[2])
                assert await finish(db, g, a, {"input_tokens": 20, "output_tokens": 30}, response=JSONResponse(result))

        await asyncio.wait_for(asyncio.gather(*(complete() for _ in range(10))), timeout=20)
        async with factory() as db:
            p, g = await db.get(Partner, ids[0]), await db.get(Generation, ids[1])
            assert p.balance_rub == Decimal("99.95") and p.cost_coverage_rub == Decimal("99.99")
            assert g.actual_charge_rub == Decimal(".05") and g.billing_status == "charged"
            for model in (LedgerEntry, WebhookEvent, NativeResponse):
                assert (
                    await db.scalar(select(func.count()).select_from(model).where(model.generation_id == ids[1])) == 1
                )
            recovered = await replay_response(db, g)
            assert recovered.status_code == 200 and recovered.body == JSONResponse(result).body
    finally:
        await cleanup(factory, ids)
        await engine.dispose()
