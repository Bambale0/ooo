"""Final failed video outcomes and refund regression under procurement debt."""
import asyncio
import os
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_generation_failure_retry import _seed
from test_generation_failure_retry import adapter as adapter

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import apply_cost_coverage_change
from app.generations.models import Generation
from app.generations.service import cancel_before_submit, dispatch_generation_to_provider, poll_generation_provider
from app.inference.accounting import settle_actual
from app.providers.base import ProviderAdapterError, ProviderPollResult
from app.providers.models import ProviderAttempt, ProviderCredential
from app.webhooks.models import WebhookEvent
from app.workers.generation_worker import _poll_generation_candidate


async def prepare(db, coverage=Decimal("-49010.29")):
    partner, generation, attempt, _ = await _seed(db)
    partner.cost_coverage_rub = coverage
    generation.mode = "videos/generations"
    generation.request_payload = {
        "protocol": "videos/generations", "partner_billing_policy": "actual_only",
        "rates": {"seconds": {"retail": "16", "cost": "0.483"}},
    }
    generation.provider_cost_usdt_snapshot = Decimal("2.415")
    generation.provider_cost_reserve_usdt = Decimal("2.415")
    generation.provider_cost_reserve_rub = Decimal("204.07")
    generation.rub_per_usdt_snapshot = Decimal("84.5")
    generation.webhook_url_snapshot = "https://partner.example.test/webhook"
    await apply_cost_coverage_change(
        db, partner, Decimal("-204.07"), "provider_cost_reserve",
        f"provider-cost-reserve:{generation.id}", generation.id,
    )
    await db.commit()
    return partner, generation, attempt


@pytest.mark.parametrize("coverage", [Decimal("-49010.29"), Decimal("1000")])
async def test_free_failure_finalizes_immediately_with_existing_debt(db_session, adapter, coverage):
    partner, generation, attempt = await prepare(db_session, coverage)
    await poll_generation_provider(db_session, generation)
    await db_session.commit()
    assert generation.status == "failed"
    assert generation.billing_status == "not_chargeable"
    assert generation.client_finalized_at is not None
    assert generation.actual_charge_rub is None
    assert generation.actual_provider_cost_usdt == 0
    assert attempt.cost_status == "confirmed_free"
    finalized_at = generation.client_finalized_at
    for _ in range(3):
        await poll_generation_provider(db_session, generation)
        await dispatch_generation_to_provider(db_session, generation)
    await db_session.commit()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert partner.cost_coverage_rub == coverage
    assert generation.client_finalized_at == finalized_at
    adapter.poll_generation.assert_awaited_once()
    adapter.submit_generation.assert_not_awaited()
    assert await db_session.scalar(select(func.sum(CoverageLedgerEntry.amount_rub)).where(
        CoverageLedgerEntry.generation_id == generation.id
    )) == Decimal("0")
    events = list(await db_session.scalars(select(WebhookEvent).where(WebhookEvent.generation_id == generation.id)))
    assert len(events) == 1 and events[0].event_type == "failed"


async def test_closed_video_rejects_late_client_charge(db_session, adapter):
    partner, generation, _ = await prepare(db_session, Decimal("1000"))
    await poll_generation_provider(db_session, generation)
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await settle_actual(db_session, generation, {"seconds": 5})
    assert error.value.status_code == 409 and error.value.detail == "generation_not_chargeable"
    await db_session.rollback()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_above_reserve_provider_cost_does_not_reopen_failed_client(db_session, adapter):
    partner, generation, attempt = await prepare(db_session, Decimal("1000"))
    adapter.poll_generation.return_value = ProviderPollResult(status="failed", usage={"provider_cost_usdt": "3"})
    await poll_generation_provider(db_session, generation)
    await db_session.commit()
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.client_finalized_at is not None
    assert generation.actual_provider_cost_usdt is None
    assert attempt.provider_cost_usdt == Decimal("3") and attempt.cost_status == "reported"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert partner.cost_coverage_rub == Decimal("795.93")


async def test_submit_rejection_has_final_billing(db_session, adapter):
    partner, generation, attempt = await prepare(db_session)
    generation.status = "queued"
    attempt.status = "retry_pending"
    attempt.provider_task_id = None
    await db_session.commit()
    adapter.submit_generation.side_effect = ProviderAdapterError("provider_rejected_request", retryable=False)
    await dispatch_generation_to_provider(db_session, generation)
    await db_session.commit()
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.client_finalized_at is not None
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_cancel_before_submit_has_final_billing(db_session, adapter):
    partner, generation, attempt = await prepare(db_session)
    generation.status = "queued"
    attempt.status = "retry_pending"
    attempt.provider_task_id = None
    await db_session.commit()
    await cancel_before_submit(db_session, generation)
    await db_session.commit()
    assert generation.status == "cancelled" and generation.billing_status == "not_chargeable"
    assert generation.client_finalized_at is not None
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert partner.cost_coverage_rub == Decimal("-49010.29")


async def test_outbox_failure_rolls_back_refund_and_retry_applies_once(db_session, adapter, monkeypatch):
    partner, generation, _ = await prepare(db_session)
    from app.generations import service
    original = service.ensure_terminal_webhook_event
    async def broken(*args, **kwargs):
        raise RuntimeError("simulated outbox failure")
    monkeypatch.setattr(service, "ensure_terminal_webhook_event", broken)
    with pytest.raises(RuntimeError, match="simulated outbox failure"):
        await poll_generation_provider(db_session, generation)
    await db_session.rollback()
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    assert generation.status == "sent_to_provider" and generation.billing_status == "pending"
    assert partner.cost_coverage_rub == Decimal("-49214.36")
    monkeypatch.setattr(service, "ensure_terminal_webhook_event", original)
    await poll_generation_provider(db_session, generation)
    await db_session.commit()
    await db_session.refresh(partner)
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert partner.cost_coverage_rub == Decimal("-49010.29")


async def test_failed_video_with_reported_cost_refunds_unused_coverage_under_debt(db_session, adapter):
    partner, generation, attempt = await prepare(db_session)
    adapter.poll_generation.return_value = ProviderPollResult(status="failed", usage={"provider_cost_usdt": "0.5"})
    await poll_generation_provider(db_session, generation)
    await db_session.commit()
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.actual_provider_cost_usdt == Decimal("0.5")
    assert attempt.provider_cost_usdt == Decimal("0.5")
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert partner.cost_coverage_rub == Decimal("-49052.54")


async def test_unknown_fallback_cost_stays_internal_after_final_client_failure(db_session, adapter):
    partner, generation, attempt = await prepare(db_session)
    attempt.provider = "infai"
    credential = await db_session.get(ProviderCredential, attempt.credential_id)
    credential.provider = "infai"
    credential.partner_id = None
    credential.label = "infai-seedance-1"
    attempt.cost_reserve_usdt = Decimal("2.415")
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    await db_session.commit()
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert attempt.cost_status == "unknown" and attempt.provider_cost_usdt is None
    assert generation.actual_provider_cost_usdt is None
    assert generation.provider_cost_hold_usdt == Decimal("2.415")
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    assert partner.cost_coverage_rub == Decimal("-49214.36")


@pytest.mark.integration
async def test_postgres_racing_workers_finalize_and_refund_once(adapter):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    generation_id = partner_id = None
    try:
        async with sessions() as db:
            partner, generation, _ = await prepare(db)
            partner_id, generation_id = partner.id, generation.id
        results = await asyncio.gather(*[
            _poll_generation_candidate(session_factory=sessions, generation_id=generation_id, provider="argolink")
            for _ in range(2)
        ])
        assert sorted(results) == [False, True]
        adapter.poll_generation.assert_awaited_once()
        adapter.submit_generation.assert_not_awaited()
        async with sessions() as db:
            generation = await db.get(Generation, generation_id)
            partner = await db.get(Partner, partner_id)
            assert generation.status == "failed" and generation.billing_status == "not_chargeable"
            assert generation.client_finalized_at is not None
            assert partner.balance_rub == Decimal("100")
            assert partner.cost_coverage_rub == Decimal("-49010.29")
            assert await db.scalar(select(func.count()).select_from(CoverageLedgerEntry).where(
                CoverageLedgerEntry.generation_id == generation_id,
                CoverageLedgerEntry.operation_type == "provider_cost_reserve_adjustment",
            )) == 1
            assert await db.scalar(select(func.count()).select_from(WebhookEvent).where(
                WebhookEvent.generation_id == generation_id
            )) == 1
    finally:
        if generation_id:
            async with sessions() as db:
                for model in (WebhookEvent, ProviderAttempt, LedgerEntry, CoverageLedgerEntry):
                    await db.execute(delete(model).where(model.generation_id == generation_id))
                await db.execute(delete(Generation).where(Generation.id == generation_id))
                await db.execute(delete(ProviderCredential).where(ProviderCredential.partner_id == partner_id))
                await db.execute(delete(Partner).where(Partner.id == partner_id))
                await db.commit()
        await engine.dispose()
