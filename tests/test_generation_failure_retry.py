import asyncio
import os
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.base import ProviderAdapterError, ProviderPollResult, ProviderSubmitResult
from app.providers.models import ProviderAttempt, ProviderCredential
from app.webhooks.models import WebhookEvent
from app.workers.generation_worker import (
    _dispatch_generation_candidate,
    _fair_due_active_candidate_ids_query,
    _poll_active_generations,
)


def _failure():
    return ProviderPollResult(
        status="failed", raw_error="internal provider failure", error_code="internal_error", retryable_failure=True
    )


@pytest.fixture
def adapter(monkeypatch):
    fake = SimpleNamespace(
        poll_generation=AsyncMock(return_value=_failure()),
        submit_generation=AsyncMock(side_effect=[ProviderSubmitResult(f"retry-{i}") for i in range(3)]),
        normalize_error=lambda error: error,
        keys=[],
    )

    def get_adapter(provider, *, api_key):
        fake.keys.append(api_key)
        return fake

    monkeypatch.setattr("app.providers.service.get_provider_adapter", get_adapter)
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter", lambda *args: SimpleNamespace(acquire=AsyncMock())
    )
    monkeypatch.setattr("app.providers.circuit.admit", AsyncMock(return_value=True))
    monkeypatch.setattr("app.providers.circuit.observe", AsyncMock())
    monkeypatch.setattr(get_settings(), "worker_generation_max_retries", 2)
    return fake


def _credential(partner_id, key):
    return ProviderCredential(
        partner_id=partner_id,
        provider="argolink",
        label="retry fixture",
        key_hash=hash_secret(key),
        key_prefix=key[:8],
        encrypted_api_key=encrypt_secret(key, get_settings().provider_credentials_master_key),
        is_active=True,
    )


async def _seed(db):
    key = f"test-retry-key-{uuid4()}"
    partner = Partner(telegram_id=str(uuid4()), company_name="Retry", project_name="Retry", balance_rub=Decimal("100"))
    db.add(partner)
    await db.flush()
    credential = _credential(partner.id, key)
    generation = Generation(
        partner_id=partner.id,
        model_id="retry-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=str(uuid4()),
        partner_price_rub=Decimal("80"),
        prompt="retry the same request",
        status="sent_to_provider",
        request_payload={"reference_images": []},
    )
    db.add_all([credential, generation])
    await db.flush()
    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        credential_id=credential.id,
        provider_task_id="original-failed-task",
        status="accepted",
    )
    db.add(attempt)
    await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=Decimal("-80"),
        operation_type="generation_reserve",
        idempotency_key=f"generation-reserve:{generation.id}",
        generation_id=generation.id,
        allow_negative=False,
    )
    await db.commit()
    return partner, generation, attempt, key


async def _ledger(db, generation):
    rows = await db.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id))
    return sorted((entry.operation_type, Decimal(entry.amount_rub)) for entry in rows)


async def test_failed_task_restarts_with_pinned_credential_and_one_charge(db_session, adapter):
    partner, generation, attempt, original_key = await _seed(db_session)
    generation_id, attempt_id, credential_id = generation.id, attempt.id, attempt.credential_id
    before = utc_now()
    await poll_generation_provider(db_session, generation)
    assert (generation.status, attempt.status, attempt.provider_task_id) == ("queued", "retry_pending", None)
    deadline = attempt.next_attempt_at.replace(tzinfo=UTC)
    assert 4.5 <= (deadline - before).total_seconds() <= 6
    history = generation.request_payload["provider_failed_tasks"]
    assert len(history) == 1
    assert history[0]["provider_task_id"] == "original-failed-task"
    assert history[0]["credential_id"] == credential_id
    assert history[0]["cost_status"] == "unknown"
    assert history[0]["error_code"] == "internal_error"
    assert history[0]["failed_at"]
    assert await _ledger(db_session, generation) == [("generation_reserve", Decimal("-80"))]
    await dispatch_generation_to_provider(db_session, generation)
    adapter.submit_generation.assert_not_awaited()

    newer = _credential(partner.id, f"new-key-{uuid4()}")
    newer.created_at = utc_now() + timedelta(seconds=1)
    db_session.add(newer)
    attempt.next_attempt_at = utc_now() - timedelta(seconds=1)
    await db_session.commit()
    db_session.expunge_all()
    generation = await db_session.get(Generation, generation_id)
    assert generation.request_payload["provider_failed_tasks"] == history
    attempt = await dispatch_generation_to_provider(db_session, generation)
    assert (attempt.id, attempt.credential_id) == (attempt_id, credential_id)
    assert adapter.keys[-1] == original_key
    assert adapter.submit_generation.await_args.args[0].generation_id == generation_id
    attempt.next_poll_at = None
    adapter.poll_generation.return_value = ProviderPollResult(status="completed")
    await poll_generation_provider(db_session, generation)
    await poll_generation_provider(db_session, generation)
    assert generation.status == "completed"
    assert adapter.submit_generation.await_count == 1
    assert await _ledger(db_session, generation) == [("generation_reserve", Decimal("-80"))]


async def test_retry_cap_releases_reserve_once_and_does_not_reopen_terminal_failure(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    for index in range(3):
        attempt.next_poll_at = None
        before = utc_now()
        await poll_generation_provider(db_session, generation)
        if index < 2:
            assert generation.status == "queued"
            delay = (attempt.next_attempt_at.replace(tzinfo=UTC) - before).total_seconds()
            assert (5, 15)[index] - 0.5 <= delay <= (5, 15)[index] + 1
            attempt.next_attempt_at = utc_now() - timedelta(seconds=1)
            await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "failed"
    await poll_generation_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)
    assert adapter.submit_generation.await_count == 2
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


@pytest.mark.parametrize("disabled", [False, True])
async def test_nonretryable_failure_or_disabled_retry_remains_terminal(db_session, adapter, monkeypatch, disabled):
    _, generation, _, _ = await _seed(db_session)
    if disabled:
        monkeypatch.setattr(get_settings(), "worker_generation_max_retries", 0)
    else:
        adapter.poll_generation.return_value = ProviderPollResult(status="failed", raw_error="rejected")
    await poll_generation_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "failed"
    adapter.submit_generation.assert_not_awaited()


async def test_unknown_retry_submit_outcome_is_quarantined_without_another_post_or_refund(db_session, adapter):
    _, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    attempt.next_attempt_at = None
    adapter.submit_generation.side_effect = ProviderAdapterError(
        "provider_temporarily_unavailable", "argolink_submit_outcome_unknown", retryable=False
    )
    await dispatch_generation_to_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "reconciliation_required"
    assert adapter.submit_generation.await_count == 1
    assert generation.request_payload["provider_failed_tasks"][0]["provider_task_id"] == "original-failed-task"
    assert await _ledger(db_session, generation) == [("generation_reserve", Decimal("-80"))]


async def test_expired_original_deadline_prevents_retry_submission(db_session, adapter):
    _, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    attempt.next_attempt_at = None
    generation.created_at = attempt.created_at = utc_now() - timedelta(minutes=31)
    await dispatch_generation_to_provider(db_session, generation)
    adapter.submit_generation.assert_not_awaited()
    assert generation.status == "timeout"
    candidates = list(await db_session.scalars(_fair_due_active_candidate_ids_query(provider="argolink", limit=10)))
    polled = await _poll_active_generations(db_session, provider="argolink", limit=10)
    assert (candidates, polled) == ([], 0)


async def test_revoked_original_credential_cancels_without_switching_keys_or_claiming_zero_provider_cost(
    db_session, adapter
):
    partner, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    original = await db_session.get(ProviderCredential, attempt.credential_id)
    original.is_active = False
    db_session.add(_credential(partner.id, f"replacement-key-{uuid4()}"))
    attempt.next_attempt_at = None
    await db_session.commit()

    await dispatch_generation_to_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)

    adapter.submit_generation.assert_not_awaited()
    assert (generation.status, attempt.status) == ("cancelled", "cancelled")
    assert generation.actual_charge_rub == Decimal("0")
    assert generation.actual_provider_cost_usdt is None
    assert generation.request_payload["provider_failed_tasks"][0]["cost_status"] == "unknown"
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_native_retry_settles_actual_usage_once_and_emits_only_one_completed_webhook(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    partner.cost_coverage_rub = Decimal("100")
    generation.request_payload = {
        "protocol": "videos/generations",
        "rates": {"seconds": {"retail": "16", "cost": "0.1"}},
    }
    generation.provider_cost_usdt_snapshot = Decimal("0.5")
    generation.provider_cost_reserve_rub = Decimal("50")
    generation.rub_per_usdt_snapshot = Decimal("100")
    generation.webhook_url_snapshot = "https://partner.example.test/webhook"
    await apply_cost_coverage_change(
        db_session,
        partner,
        Decimal("-50"),
        "provider_cost_reserve",
        f"provider-cost-reserve:{generation.id}",
        generation.id,
        allow_negative=False,
    )
    await poll_generation_provider(db_session, generation)
    events = select(WebhookEvent).where(WebhookEvent.generation_id == generation.id)
    assert list(await db_session.scalars(events)) == []
    assert await _ledger(db_session, generation) == [("generation_reserve", Decimal("-80"))]
    attempt.next_attempt_at = None
    await dispatch_generation_to_provider(db_session, generation)
    assert list(await db_session.scalars(events)) == []
    attempt.next_poll_at = None
    adapter.poll_generation.return_value = ProviderPollResult(status="completed", usage={"billed_seconds": 5})
    await poll_generation_provider(db_session, generation)
    await poll_generation_provider(db_session, generation)
    assert generation.actual_charge_rub == Decimal("80")
    assert generation.actual_provider_cost_usdt == Decimal("0.5")
    assert generation.usage_snapshot == {"seconds": 5}
    assert generation.request_payload["provider_failed_tasks"][0]["cost_status"] == "unknown"
    assert [event.event_type for event in await db_session.scalars(events)] == ["completed"]
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_usage_adjustment", Decimal("0")),
    ]
    coverage = await db_session.scalars(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
    )
    assert sorted((entry.operation_type, entry.amount_rub) for entry in coverage) == [
        ("provider_cost_reserve", Decimal("-50")),
        ("provider_usage_adjustment", Decimal("0")),
    ]
    await db_session.refresh(partner)
    assert (partner.balance_rub, partner.cost_coverage_rub) == (Decimal("20"), Decimal("50"))


@pytest.mark.integration
async def test_postgres_workers_claim_one_retry_submission_after_restart(adapter):
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    partner_id = generation_id = None
    try:
        async with sessions() as db:
            partner, generation, attempt, _ = await _seed(db)
            partner_id, generation_id = partner.id, generation.id
            await poll_generation_provider(db, generation)
            attempt.next_attempt_at = utc_now() - timedelta(seconds=1)
            await db.commit()

        async def delayed_submit(payload):
            await asyncio.sleep(0.05)
            return ProviderSubmitResult("one-retry-task")

        adapter.submit_generation.side_effect = delayed_submit
        await asyncio.gather(
            *[
                _dispatch_generation_candidate(
                    session_factory=sessions, generation_id=generation_id, provider="argolink"
                )
                for _ in range(2)
            ]
        )
        assert adapter.submit_generation.await_count == 1
        async with sessions() as db:
            generation = await db.get(Generation, generation_id)
            assert generation.status == "sent_to_provider"
            assert len(generation.request_payload["provider_failed_tasks"]) == 1
            assert await _ledger(db, generation) == [("generation_reserve", Decimal("-80"))]
    finally:
        if generation_id:
            async with sessions() as db:
                for model in (ProviderAttempt, LedgerEntry):
                    await db.execute(delete(model).where(model.generation_id == generation_id))
                await db.execute(delete(Generation).where(Generation.id == generation_id))
                await db.execute(delete(ProviderCredential).where(ProviderCredential.partner_id == partner_id))
                await db.execute(delete(Partner).where(Partner.id == partner_id))
                await db.commit()
        await engine.dispose()
