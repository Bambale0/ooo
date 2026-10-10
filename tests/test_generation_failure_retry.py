import asyncio
import os
from datetime import timedelta
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


async def test_failed_accepted_task_is_never_resubmitted_to_same_provider(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    assert (generation.status, attempt.status, attempt.provider_task_id) == (
        "failed",
        "failed",
        "original-failed-task",
    )
    assert attempt.cost_status == "confirmed_free"
    await dispatch_generation_to_provider(db_session, generation)
    adapter.submit_generation.assert_not_awaited()
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_terminal_failed_task_stays_terminal_and_releases_retail_once(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    for _ in range(3):
        await poll_generation_provider(db_session, generation)
        await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "failed"
    assert attempt.provider_task_id == "original-failed-task"
    adapter.submit_generation.assert_not_awaited()
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


async def test_terminal_failed_task_never_enters_a_second_submit_path(db_session, adapter):
    _, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    adapter.submit_generation.side_effect = ProviderAdapterError(
        "provider_temporarily_unavailable", "argolink_submit_outcome_unknown", retryable=False
    )
    await dispatch_generation_to_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "failed"
    adapter.submit_generation.assert_not_awaited()
    assert attempt.provider_task_id == "original-failed-task"
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]


async def test_terminal_failure_has_no_retry_deadline(db_session, adapter):
    _, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    generation.created_at = attempt.created_at = utc_now() - timedelta(minutes=31)
    await dispatch_generation_to_provider(db_session, generation)
    adapter.submit_generation.assert_not_awaited()
    assert generation.status == "failed"
    assert attempt.next_attempt_at is None
    candidates = list(await db_session.scalars(_fair_due_active_candidate_ids_query(provider="argolink", limit=10)))
    polled = await _poll_active_generations(db_session, provider="argolink", limit=10)
    assert (candidates, polled) == ([], 0)


async def test_definitive_late_provider_failure_replaces_timeout_status(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    attempt.created_at = utc_now() - timedelta(minutes=31)
    await db_session.commit()

    await poll_generation_provider(db_session, generation)
    assert (generation.status, attempt.status, generation.public_error_code) == (
        "timeout",
        "timeout",
        "generation_timeout",
    )

    attempt.next_poll_at = utc_now() - timedelta(seconds=1)
    adapter.poll_generation.return_value = ProviderPollResult(
        status="failed",
        raw_error="The result could not be generated.",
    )
    await db_session.commit()

    await poll_generation_provider(db_session, generation)

    assert (generation.status, attempt.status, generation.public_error_code) == (
        "failed",
        "failed",
        "provider_generation_failed",
    )
    assert attempt.raw_error == "The result could not be generated."
    assert attempt.cost_status == "confirmed_free"
    assert generation.actual_provider_cost_usdt == Decimal("0")
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_revoked_original_credential_does_not_change_terminal_failure_or_switch_keys(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    await poll_generation_provider(db_session, generation)
    original = await db_session.get(ProviderCredential, attempt.credential_id)
    original.is_active = False
    db_session.add(_credential(partner.id, f"replacement-key-{uuid4()}"))
    await db_session.commit()

    await dispatch_generation_to_provider(db_session, generation)
    await dispatch_generation_to_provider(db_session, generation)

    adapter.submit_generation.assert_not_awaited()
    assert (generation.status, attempt.status) == ("failed", "failed")
    assert generation.actual_charge_rub is None
    assert generation.actual_provider_cost_usdt == Decimal("0")
    assert attempt.cost_status == "confirmed_free"
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")


async def test_native_terminal_failure_closes_confirmed_free_cost_and_emits_one_failed_webhook(db_session, adapter):
    partner, generation, attempt, _ = await _seed(db_session)
    partner.cost_coverage_rub = Decimal("100")
    generation.mode = "videos/generations"
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
    assert [event.event_type for event in await db_session.scalars(events)] == ["failed"]
    await dispatch_generation_to_provider(db_session, generation)
    adapter.submit_generation.assert_not_awaited()
    assert generation.status == "failed"
    assert generation.actual_charge_rub is None
    assert generation.actual_provider_cost_usdt == Decimal("0")
    assert attempt.cost_status == "confirmed_free"
    assert await _ledger(db_session, generation) == [
        ("generation_reserve", Decimal("-80")),
        ("generation_reserve_release", Decimal("80")),
    ]
    coverage = await db_session.scalars(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
    )
    assert sorted((entry.operation_type, entry.amount_rub) for entry in coverage) == [
        ("provider_cost_reserve", Decimal("-50")),
        ("provider_cost_reserve_adjustment", Decimal("50")),
    ]
    await db_session.refresh(partner)
    assert (partner.balance_rub, partner.cost_coverage_rub) == (Decimal("100"), Decimal("100"))


@pytest.mark.integration
async def test_postgres_workers_do_not_resubmit_terminal_provider_task_after_restart(adapter):
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
            await db.commit()

        await asyncio.gather(
            *[
                _dispatch_generation_candidate(
                    session_factory=sessions, generation_id=generation_id, provider="argolink"
                )
                for _ in range(2)
            ]
        )
        adapter.submit_generation.assert_not_awaited()
        async with sessions() as db:
            generation = await db.get(Generation, generation_id)
            assert generation.status == "failed"
            assert await _ledger(db, generation) == [
                ("generation_reserve", Decimal("-80")),
                ("generation_reserve_release", Decimal("80")),
            ]
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
