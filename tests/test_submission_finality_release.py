"""Unknown video submission must not leave the customer's request pending forever."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt
from app.webhooks.models import WebhookEvent
from app.workers.generation_worker import process_generation_work_once


async def seed(db, *, age=3600, policy="actual_only", task_id=None, status="reconciliation_required"):
    partner = Partner(
        telegram_id="submission-audit", company_name="Test", project_name="Test", balance_rub=Decimal("50")
    )
    db.add(partner)
    await db.flush()
    g = Generation(
        partner_id=partner.id,
        model_id="m",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=5,
        prompt="test",
        idempotency_key="unknown-submit",
        partner_price_rub=Decimal("20"),
        provider_cost_usdt_snapshot=Decimal("0.15"),
        provider_cost_reserve_usdt=Decimal("0.15"),
        provider_cost_reserve_rub=Decimal("15"),
        rub_per_usdt_snapshot=Decimal("100"),
        status=status,
        request_payload={"partner_billing_policy": policy, "protocol": "videos/generations"},
        public_error_code="submission_outcome_unknown",
        created_at=utc_now() - timedelta(seconds=age),
        webhook_url_snapshot="https://example.org/hooks",
    )
    db.add(g)
    await db.flush()
    a = ProviderAttempt(
        generation_id=g.id,
        provider="argolink",
        status="reconciliation_required",
        provider_task_id=task_id,
        cost_reserve_usdt=Decimal("0.15"),
        created_at=utc_now() - timedelta(seconds=age),
    )
    db.add(a)
    await db.commit()
    return partner, g, a


async def test_worker_finalizes_unknown_submit_without_customer_charge_or_cost_writeoff(db_session):
    partner, g, a = await seed(db_session)
    await process_generation_work_once(db_session)
    await db_session.commit()
    assert g.status == "failed" and g.billing_status == "not_chargeable"
    assert g.client_finalized_at is not None and g.actual_charge_rub is None
    assert a.status == "reconciliation_required" and a.cost_status == "unknown"
    assert a.cost_reserve_usdt == g.provider_cost_hold_usdt == Decimal("0.15")
    await process_generation_work_once(db_session)
    await db_session.commit()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("50")
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WebhookEvent)) == 1


@pytest.mark.parametrize("changes", [{"age": 10}, {"policy": "legacy"}, {"task_id": "known-task"}])
async def test_worker_does_not_finalize_live_historical_or_known_provider_jobs(db_session, changes):
    _, g, _ = await seed(db_session, **changes)
    await process_generation_work_once(db_session)
    assert g.status == "reconciliation_required" and g.client_finalized_at is None


async def test_newer_attempt_is_not_expired_using_older_generation_timestamp(db_session):
    _, g, a = await seed(db_session)
    a.created_at = utc_now()
    await db_session.commit()
    await process_generation_work_once(db_session)
    assert g.status == "reconciliation_required"


async def test_abandoned_submit_intent_is_finalized_without_resubmission(db_session):
    _, g, a = await seed(db_session, status="sent_to_provider")
    a.status = "submitting"
    await db_session.commit()
    await process_generation_work_once(db_session)
    assert g.status == "failed" and g.billing_status == "not_chargeable"
    assert a.provider_task_id is None and a.cost_status == "unknown"


@pytest.mark.parametrize("late_failure", [False, True])
async def test_late_submit_reply_cannot_reopen_customer_finality(db_session, monkeypatch, late_failure):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.generations.service import dispatch_generation_to_provider
    from app.generations.submission_recovery import recover_unconfirmed_submissions_once
    from app.providers.base import ProviderAdapterError, ProviderSubmitResult

    _, g, a = await seed(db_session, status="queued")
    a.status = "retry_pending"
    await db_session.commit()

    async def submit(request):
        assert g.status == "sent_to_provider"
        assert await recover_unconfirmed_submissions_once(db_session) == 1
        await db_session.commit()
        if late_failure:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable", "argolink_submit_outcome_unknown", retryable=False
            )
        return ProviderSubmitResult(provider_task_id="late-accepted-id", status="accepted", raw_response={})

    adapter = SimpleNamespace(submit_generation=submit, normalize_error=lambda error: error)
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr("app.generations.service.get_active_provider_credential", AsyncMock(return_value=None))
    monkeypatch.setattr("app.providers.circuit.admit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter", lambda *args: SimpleNamespace(acquire=AsyncMock())
    )
    await dispatch_generation_to_provider(db_session, g)
    await db_session.commit()
    assert g.status == "failed" and g.billing_status == "not_chargeable"
    assert a.status == "reconciliation_required" and a.cost_status == "unknown"
    assert a.provider_task_id == (None if late_failure else "late-accepted-id")
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
