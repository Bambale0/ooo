from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change
from app.generations.models import Generation
from app.generations.service import poll_generation_provider
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderAdapterError
from app.providers.models import ProviderAttempt
from app.workers.generation_worker import _fair_due_active_candidate_ids_query, _poll_generation_candidate


async def seeded(db, *, review=False, protocol=None, task_id="known-video"):
    partner = Partner(telegram_id=str(uuid4()), company_name="Test", project_name="Test", balance_rub=Decimal("100"))
    db.add(partner)
    await db.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="test",
        model_slug="seedance-2.5",
        mode=protocol or "text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=str(uuid4()),
        partner_price_rub=Decimal("80"),
        prompt="fixture",
        status="reconciliation_required" if review else "processing",
        public_error_code="usage_reconciliation_required" if review else None,
        request_payload={"protocol": protocol} if protocol else {},
    )
    db.add(generation)
    await db.flush()
    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id=task_id,
        status=generation.status,
        retry_count=get_settings().worker_max_retries,
        created_at=utc_now() - timedelta(hours=2) if review else utc_now(),
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
    return partner, generation, attempt


def adapter_for(monkeypatch, outcome):
    adapter = SimpleNamespace(
        poll_generation=AsyncMock(
            side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome
        ),
        normalize_error=lambda error: error,
    )
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter", lambda *_: SimpleNamespace(acquire=AsyncMock())
    )
    return adapter


def result(status="processing", *, verified=True, usage=None):
    return SimpleNamespace(
        status=status,
        result_url=None,
        raw_error=None,
        usage=usage,
        error_code=None,
        retryable_failure=False,
        task_identity_verified=verified,
    )


@pytest.mark.parametrize("retryable", [True, False])
async def test_known_argolink_get_error_never_becomes_terminal_at_retry_cap(db_session, monkeypatch, retryable):
    partner, generation, attempt = await seeded(db_session)
    adapter_for(
        monkeypatch, ProviderAdapterError("provider_temporarily_unavailable", "http_failure", retryable=retryable)
    )
    await poll_generation_provider(db_session, generation)
    await db_session.refresh(partner)
    assert generation.status == "processing"
    assert attempt.provider_task_id == "known-video"
    assert attempt.next_attempt_at is not None
    assert partner.balance_rub == Decimal("20")
    ledger = list(
        (await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id))).all()
    )
    assert [entry.operation_type for entry in ledger] == ["generation_reserve"]


@pytest.mark.parametrize("request_id", ["wrong-video", None, 17, ""])
async def test_poll_rejects_present_mismatched_or_malformed_identity(request_id):
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"request_id": request_id, "status": "done"})),
    ) as client:
        with pytest.raises(ProviderAdapterError):
            await ArgoLinkAdapter(api_key="fixture", client=client).poll_generation("known-video")


async def test_explicit_same_id_marks_verified_result():
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"request_id": "known-video", "status": "pending"})
        ),
    ) as client:
        status = await ArgoLinkAdapter(api_key="fixture", client=client).poll_generation("known-video")
    assert getattr(status, "task_identity_verified", False)


@pytest.mark.parametrize("protocol", [None, "videos/generations"])
async def test_known_usage_review_is_scheduled(db_session, protocol):
    _, generation, _ = await seeded(db_session, review=True, protocol=protocol)
    ids = list((await db_session.scalars(_fair_due_active_candidate_ids_query(provider=None, limit=20))).all())
    assert generation.id in ids


@pytest.mark.parametrize(
    "protocol,task_id", [("responses", "text-id"), ("images/generations", "image-id"), (None, None), (None, "")]
)
async def test_review_never_schedules_nonvideo_or_missing_identity(db_session, protocol, task_id):
    _, generation, _ = await seeded(db_session, review=True, protocol=protocol, task_id=task_id)
    ids = list((await db_session.scalars(_fair_due_active_candidate_ids_query(provider=None, limit=20))).all())
    assert generation.id not in ids


async def test_old_usage_review_survives_repeated_get_errors_without_timeout_refund(db_session, monkeypatch):
    partner, generation, attempt = await seeded(db_session, review=True)
    adapter_for(monkeypatch, ProviderAdapterError("provider_temporarily_unavailable", "get_timeout"))
    for _ in range(2):
        attempt.next_attempt_at = None
        attempt.next_poll_at = None
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
        assert generation.status == attempt.status == "reconciliation_required"
        assert generation.public_error_code == "usage_reconciliation_required"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("20")


@pytest.mark.parametrize("verified", [False, True])
async def test_stale_unknown_clears_only_on_verified_active_result(db_session, monkeypatch, verified):
    _, generation, _ = await seeded(db_session)
    generation.public_error_code = "submission_outcome_unknown"
    adapter_for(monkeypatch, result(verified=verified))
    await poll_generation_provider(db_session, generation)
    assert generation.public_error_code == (None if verified else "submission_outcome_unknown")


async def test_old_usage_review_requires_identity_and_terminal_evidence(db_session, monkeypatch):
    partner, generation, attempt = await seeded(db_session, review=True)
    for outcome in [result("completed", verified=False), result("processing", verified=True)]:
        adapter_for(monkeypatch, outcome)
        attempt.next_attempt_at = attempt.next_poll_at = None
        await poll_generation_provider(db_session, generation)
        assert generation.status == attempt.status == "reconciliation_required"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("20")


async def test_long_running_recovery_backoff_does_not_overflow(db_session, monkeypatch):
    _, generation, attempt = await seeded(db_session, review=True)
    attempt.retry_count = 10000
    adapter_for(monkeypatch, ProviderAdapterError("provider_temporarily_unavailable", "get_timeout"))
    await poll_generation_provider(db_session, generation)
    assert generation.status == "reconciliation_required"
    assert attempt.next_attempt_at is not None


@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_restarted_worker_recovers_review_and_settles_once(db_session, monkeypatch, terminal):
    partner, generation, _ = await seeded(db_session, review=True, protocol="videos/generations")
    generation.request_payload = {"protocol": "videos/generations", "rates": {"seconds": {"retail": "16", "cost": "0"}}}
    await db_session.commit()
    adapter = adapter_for(monkeypatch, result(terminal, usage={"billed_seconds": 5}))
    adapter.submit_generation = AsyncMock(side_effect=AssertionError("Recovery must not submit"))
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    assert await _poll_generation_candidate(session_factory=factory, generation_id=generation.id, provider=None)
    assert not await _poll_generation_candidate(session_factory=factory, generation_id=generation.id, provider=None)
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    assert generation.status == terminal
    assert partner.balance_rub == Decimal("20" if terminal == "completed" else "100")
    entries = list(
        (await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id))).all()
    )
    assert sum(entry.operation_type == "generation_reserve_release" for entry in entries) == (terminal == "failed")
    assert sum(entry.operation_type == "generation_usage_adjustment" for entry in entries) == (terminal == "completed")
    adapter.poll_generation.assert_awaited_once_with("known-video")
    adapter.submit_generation.assert_not_awaited()


async def test_same_video_missing_usage_then_late_usage_recovers(db_session, monkeypatch):
    _, generation, attempt = await seeded(db_session, protocol="videos/generations")
    generation.request_payload = {"protocol": "videos/generations", "rates": {"seconds": {"retail": "16", "cost": "0"}}}
    adapter_for(monkeypatch, result("completed", usage=None))
    await poll_generation_provider(db_session, generation)
    assert generation.status == attempt.status == "reconciliation_required"
    assert generation.public_error_code == "usage_reconciliation_required"
    assert attempt.next_attempt_at is not None
    await db_session.commit()
    attempt.created_at = utc_now() - timedelta(hours=2)
    attempt.next_attempt_at = attempt.next_poll_at = None
    await db_session.commit()
    adapter_for(monkeypatch, result("completed", usage={"billed_seconds": 5}))
    await poll_generation_provider(db_session, generation)
    assert generation.status == "completed"
    assert generation.actual_charge_rub == Decimal("80")


async def test_ordinary_financial_timeout_policy_is_unchanged(db_session, monkeypatch):
    partner, generation, attempt = await seeded(db_session)
    attempt.created_at = utc_now() - timedelta(hours=2)
    adapter = adapter_for(monkeypatch, result("processing"))
    await poll_generation_provider(db_session, generation)
    assert generation.status == "timeout"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("100")
    adapter.poll_generation.assert_not_awaited()


@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_worker_retries_status_after_cap_then_finishes_once(db_session, monkeypatch, terminal):
    partner, generation, attempt = await seeded(db_session)
    adapter = adapter_for(monkeypatch, result(terminal, usage={"billed_seconds": 5}))
    adapter.poll_generation.side_effect = [
        ProviderAdapterError("provider_temporarily_unavailable", "http_503"),
        result(terminal, usage={"billed_seconds": 5}),
    ]
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    assert await _poll_generation_candidate(session_factory=factory, generation_id=generation.id, provider=None)
    await db_session.refresh(generation)
    await db_session.refresh(attempt)
    assert generation.status == "processing"
    attempt.next_attempt_at = None
    await db_session.commit()
    assert await _poll_generation_candidate(session_factory=factory, generation_id=generation.id, provider=None)
    assert not await _poll_generation_candidate(session_factory=factory, generation_id=generation.id, provider=None)
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    assert generation.status == terminal
    assert partner.balance_rub == Decimal("20" if terminal == "completed" else "100")
    assert adapter.poll_generation.await_count == 2


async def test_missing_upstream_identity_remains_unknown_without_provider_calls(db_session, monkeypatch):
    partner, generation, _ = await seeded(db_session, review=True, task_id=None)
    generation.public_error_code = "submission_outcome_unknown"
    adapter = adapter_for(monkeypatch, result("completed"))
    await poll_generation_provider(db_session, generation)
    assert generation.status == "reconciliation_required"
    assert generation.public_error_code == "submission_outcome_unknown"
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("20")
    adapter.poll_generation.assert_not_awaited()


async def test_unrelated_review_reason_is_not_scheduled_even_with_video_id(db_session):
    _, generation, _ = await seeded(db_session, review=True)
    generation.public_error_code = "submission_outcome_unknown"
    await db_session.commit()
    ids = list((await db_session.scalars(_fair_due_active_candidate_ids_query(provider=None, limit=20))).all())
    assert generation.id not in ids


async def test_explicit_fallback_admin_poll_behavior_is_preserved(db_session, monkeypatch):
    _, generation, attempt = await seeded(db_session)
    generation.status = attempt.status = "reconciliation_required"
    generation.public_error_code = "submission_outcome_unknown"
    attempt.provider = "asale"
    adapter = adapter_for(monkeypatch, result("processing", verified=False))
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "asale")
    adapter.poll_generation.assert_awaited_once_with("known-video")
    assert generation.status == "processing"


@pytest.mark.parametrize("protocol", [None, "responses", "images/generations"])
async def test_automatic_explicit_provider_cannot_bypass_review_eligibility(db_session, monkeypatch, protocol):
    _, generation, _ = await seeded(db_session, review=True, protocol=protocol)
    if protocol is None:
        generation.public_error_code = "submission_outcome_unknown"
    await db_session.commit()
    adapter = adapter_for(monkeypatch, result("completed"))
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    assert not await _poll_generation_candidate(
        session_factory=factory, generation_id=generation.id, provider="argolink"
    )
    adapter.poll_generation.assert_not_awaited()


async def test_nonzero_accounting_review_settles_original_tariff_once(db_session, monkeypatch):
    from test_infai_fallback import seed

    from app.billing.models import CoverageLedgerEntry

    partner, generation, attempt, _ = await seed(db_session)
    generation.status = attempt.status = "reconciliation_required"
    generation.public_error_code = "usage_reconciliation_required"
    attempt.created_at = utc_now() - timedelta(hours=2)
    attempt.next_attempt_at = attempt.next_poll_at = None
    await db_session.commit()
    adapter = adapter_for(monkeypatch, result("completed", usage={"billed_seconds": 15}))
    for _ in range(2):
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
    await db_session.refresh(partner)
    assert generation.status == "completed"
    assert generation.actual_charge_rub == Decimal("327.00")
    assert generation.actual_provider_cost_usdt == Decimal("2.499990000000000000")
    assert partner.balance_rub == Decimal("673.00")
    assert partner.cost_coverage_rub == Decimal("787.50")
    coverage = list(
        (
            await db_session.scalars(
                select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
            )
        ).all()
    )
    assert sorted((entry.operation_type, entry.amount_rub) for entry in coverage) == [
        ("provider_cost_reserve", Decimal("-489.17")),
        ("provider_usage_adjustment", Decimal("276.67")),
    ]
    ledger = list(
        (await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id))).all()
    )
    assert sorted((entry.operation_type, entry.amount_rub) for entry in ledger) == [
        ("generation_reserve", Decimal("-327.00")),
        ("generation_usage_adjustment", Decimal("0.00")),
    ]
    adapter.poll_generation.assert_awaited_once_with(attempt.provider_task_id)


@pytest.mark.parametrize("usage", [None, ["bad"], "bad", 17])
async def test_repeated_missing_usage_preserves_backoff_counter(db_session, monkeypatch, usage):
    _, generation, attempt = await seeded(db_session, review=True, protocol="videos/generations")
    generation.request_payload = {"protocol": "videos/generations", "rates": {"seconds": {"retail": "16", "cost": "0"}}}
    adapter_for(monkeypatch, result("completed", usage=usage))
    initial = attempt.retry_count
    for index in range(2):
        attempt.next_attempt_at = attempt.next_poll_at = None
        await poll_generation_provider(db_session, generation)
        assert attempt.retry_count == initial + index + 1
        assert generation.status == "reconciliation_required"
