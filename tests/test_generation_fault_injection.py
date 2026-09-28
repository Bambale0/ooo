import asyncio
import os
from datetime import UTC
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change
from app.generations.models import Generation
from app.providers.base import ProviderPollResult, ProviderSubmitResult
from app.providers.models import ProviderAttempt
from app.workers.generation_worker import _dispatch_generation_candidate


class _NoopRateLimiter:
    async def acquire(self) -> None:
        return None


class _CountingProviderAdapter:
    provider_name = "argolink"

    def __init__(self) -> None:
        self.submit_calls = 0

    async def health_check(self) -> bool:
        return True

    async def validate_key(self, api_key: str) -> bool:
        return True

    async def submit_generation(self, payload) -> ProviderSubmitResult:
        self.submit_calls += 1
        # Give a competing worker time to reach the same row lock.
        await asyncio.sleep(0.05)
        return ProviderSubmitResult(provider_task_id=f"task-{payload.generation_id}")

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult:
        return ProviderPollResult(status="processing")

    async def open_result_stream(self, provider_content_url: str, *, range_header: str | None = None):
        raise AssertionError("result streaming is not part of this fault schedule")

    def normalize_error(self, error: Exception):
        raise error


async def _cleanup_fault_fixture(session_factory, *, partner_id: str, generation_id: str) -> None:
    async with session_factory() as session:
        await session.execute(delete(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id))
        await session.execute(delete(LedgerEntry).where(LedgerEntry.partner_id == partner_id))
        generation = await session.get(Generation, generation_id)
        if generation is not None:
            await session.delete(generation)
        partner = await session.get(Partner, partner_id)
        if partner is not None:
            await session.delete(partner)
        await session.commit()


def _postgres_session_factory():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.integration
async def test_restart_after_reserve_before_submit_resumes_without_duplicate_charge(monkeypatch):
    """Fault schedule: API commits reserve, process disappears, a fresh worker resumes the queued job."""

    engine, session_factory = _postgres_session_factory()
    partner_id: str | None = None
    generation_id: str | None = None
    adapter = _CountingProviderAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter",
        lambda provider, operation: _NoopRateLimiter(),
    )

    try:
        async with session_factory() as session:
            partner = Partner(
                telegram_id=f"fault-resume-{uuid4()}",
                company_name="Fault Resume Partner",
                project_name="Fault Resume Bot",
                balance_rub=Decimal("100.00"),
            )
            session.add(partner)
            await session.flush()
            partner_id = partner.id

            generation = Generation(
                partner_id=partner.id,
                model_id="fault-model",
                model_slug="seedance-2.5",
                mode="text_to_video",
                resolution="720p",
                duration_seconds=5,
                idempotency_key=f"fault-resume-{uuid4()}",
                partner_price_rub=Decimal("80.00"),
                prompt="durable resume",
                status="queued",
            )
            session.add(generation)
            await session.flush()
            generation_id = generation.id

            await apply_partner_balance_change(
                db=session,
                partner=partner,
                amount_rub=Decimal("-80.00"),
                operation_type="generation_reserve",
                idempotency_key=f"generation-reserve:{generation.id}",
                generation_id=generation.id,
                allow_negative=False,
            )
            await session.commit()

        # New DB session represents a restarted worker with no in-memory state from the API process.
        dispatched = await _dispatch_generation_candidate(
            session_factory=session_factory,
            generation_id=generation_id,
            provider="argolink",
        )
        assert dispatched is True
        assert adapter.submit_calls == 1

        async with session_factory() as verify:
            partner = await verify.get(Partner, partner_id)
            generation = await verify.get(Generation, generation_id)
            assert partner is not None
            assert generation is not None
            assert Decimal(partner.balance_rub) == Decimal("20.00")
            assert generation.status == "sent_to_provider"

            ledger_result = await verify.execute(select(LedgerEntry).where(LedgerEntry.generation_id == generation_id))
            ledger = list(ledger_result.scalars().all())
            assert len(ledger) == 1
            assert ledger[0].operation_type == "generation_reserve"
            assert Decimal(ledger[0].amount_rub) == Decimal("-80.00")
    finally:
        if partner_id is not None and generation_id is not None:
            await _cleanup_fault_fixture(
                session_factory,
                partner_id=partner_id,
                generation_id=generation_id,
            )
        await engine.dispose()


@pytest.mark.integration
async def test_two_workers_racing_same_generation_submit_upstream_at_most_once(monkeypatch):
    """Fault schedule: two workers independently select the same queued generation."""

    engine, session_factory = _postgres_session_factory()
    partner_id: str | None = None
    generation_id: str | None = None
    adapter = _CountingProviderAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter",
        lambda provider, operation: _NoopRateLimiter(),
    )

    try:
        async with session_factory() as session:
            partner = Partner(
                telegram_id=f"fault-race-{uuid4()}",
                company_name="Fault Race Partner",
                project_name="Fault Race Bot",
                balance_rub=Decimal("100.00"),
            )
            session.add(partner)
            await session.flush()
            partner_id = partner.id

            generation = Generation(
                partner_id=partner.id,
                model_id="fault-model",
                model_slug="seedance-2.5",
                mode="text_to_video",
                resolution="720p",
                duration_seconds=5,
                idempotency_key=f"fault-race-{uuid4()}",
                partner_price_rub=Decimal("80.00"),
                prompt="worker race",
                status="queued",
            )
            session.add(generation)
            await session.flush()
            generation_id = generation.id
            await session.commit()

        results = await asyncio.gather(
            _dispatch_generation_candidate(
                session_factory=session_factory,
                generation_id=generation_id,
                provider="argolink",
            ),
            _dispatch_generation_candidate(
                session_factory=session_factory,
                generation_id=generation_id,
                provider="argolink",
            ),
        )

        assert sorted(results) == [False, True]
        assert adapter.submit_calls == 1

        async with session_factory() as verify:
            generation = await verify.get(Generation, generation_id)
            assert generation is not None
            assert generation.status == "sent_to_provider"

            attempts_result = await verify.execute(
                select(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id)
            )
            attempts = list(attempts_result.scalars().all())
            assert len(attempts) == 1
            assert attempts[0].provider_task_id == f"task-{generation_id}"
    finally:
        if partner_id is not None and generation_id is not None:
            await _cleanup_fault_fixture(
                session_factory,
                partner_id=partner_id,
                generation_id=generation_id,
            )
        await engine.dispose()


@pytest.mark.integration
async def test_crash_after_provider_acceptance_does_not_resubmit(monkeypatch):
    """The process dies after upstream accepts but before its ID reaches our database."""
    engine, session_factory = _postgres_session_factory()
    adapter = _CountingProviderAdapter()

    class ProcessDied(BaseException):
        pass

    async def accepted_then_crashed(payload):
        adapter.submit_calls += 1
        raise ProcessDied()

    adapter.submit_generation = accepted_then_crashed

    async def get_adapter(db, partner_id, provider="argolink"):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr("app.generations.service.get_provider_rate_limiter", lambda *args: _NoopRateLimiter())
    async with session_factory() as session:
        partner = Partner(telegram_id=str(uuid4()), company_name="Crash test", project_name="Test")
        session.add(partner)
        await session.flush()
        generation = Generation(
            partner_id=partner.id,
            model_id="fault-model",
            model_slug="seedance-2.5",
            mode="text_to_video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key=str(uuid4()),
            partner_price_rub=Decimal("20"),
            prompt="crash",
            status="queued",
        )
        session.add(generation)
        await session.commit()
        partner_id, generation_id = partner.id, generation.id
    try:
        with pytest.raises(ProcessDied):
            await _dispatch_generation_candidate(
                session_factory=session_factory,
                generation_id=generation_id,
                provider="argolink",
            )
        assert (
            await _dispatch_generation_candidate(
                session_factory=session_factory,
                generation_id=generation_id,
                provider="argolink",
            )
            is False
        )
        assert adapter.submit_calls == 1
        async with session_factory() as session:
            attempt = (
                await session.execute(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id))
            ).scalar_one()
            assert attempt.status == "submitting"
            assert attempt.provider_task_id is None
    finally:
        await _cleanup_fault_fixture(session_factory, partner_id=partner_id, generation_id=generation_id)
        await engine.dispose()


class _CompletedAfterTimeoutAdapter:
    provider_name = "argolink"

    async def health_check(self) -> bool:
        return True

    async def validate_key(self, api_key: str) -> bool:
        return True

    async def submit_generation(self, payload) -> ProviderSubmitResult:
        raise AssertionError("late-success reconciliation must not submit a new provider job")

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult:
        assert provider_task_id == "late-success-task"
        return ProviderPollResult(
            status="completed",
            result_url="https://argolink.io/v1/videos/late-success-task/content",
        )

    async def open_result_stream(self, provider_content_url: str, *, range_header: str | None = None):
        raise AssertionError("result streaming is outside this reconciliation test")

    def normalize_error(self, error: Exception):
        raise error


async def test_late_provider_success_recharges_released_reserves_exactly_once(
    db_session,
    monkeypatch,
):
    from app.billing.models import CoverageLedgerEntry
    from app.billing.service import (
        apply_cost_coverage_change,
        release_generation_reserves,
    )
    from app.generations.service import poll_generation_provider
    from app.infrastructure.retry import utc_now
    from app.media.models import MediaAsset
    from app.webhooks.models import WebhookEvent
    from app.webhooks.service import ensure_terminal_webhook_event

    partner = Partner(
        telegram_id=f"late-success-{uuid4()}",
        company_name="Late Success Partner",
        project_name="Late Success Bot",
        balance_rub=Decimal("100.00"),
        cost_coverage_rub=Decimal("50.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="late-success-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=f"late-success-{uuid4()}",
        partner_price_rub=Decimal("80.00"),
        provider_cost_usdt_snapshot=Decimal("0.200000"),
        rub_per_usdt_snapshot=Decimal("100.000000"),
        provider_cost_reserve_rub=Decimal("20.00"),
        prompt="late success",
        status="sent_to_provider",
        webhook_url_snapshot="https://partner.example.test/hooks/neironych",
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
    await apply_cost_coverage_change(
        db=db_session,
        partner=partner,
        amount_rub=Decimal("-20.00"),
        operation_type="provider_cost_reserve",
        idempotency_key=f"provider-cost-reserve:{generation.id}",
        generation_id=generation.id,
        allow_negative=False,
    )

    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id="late-success-task",
        status="timeout",
        next_poll_at=utc_now(),
    )
    db_session.add(attempt)
    generation.status = "timeout"
    generation.public_error_code = "generation_timeout"

    await release_generation_reserves(
        db_session,
        generation,
        reason="timeout before provider completed",
    )
    timeout_event = await ensure_terminal_webhook_event(db_session, generation)
    assert timeout_event is not None
    assert timeout_event.event_type == "timeout"

    await db_session.flush()
    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("100.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("50.00")

    adapter = _CompletedAfterTimeoutAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        assert partner_id == partner.id
        assert provider == "argolink"
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter",
        lambda provider, operation: _NoopRateLimiter(),
    )

    first = await poll_generation_provider(db_session, generation, "argolink")
    second = await poll_generation_provider(db_session, generation, "argolink")

    assert first.status == "completed"
    assert second.status == "completed"
    assert generation.public_error_code is None

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("20.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("30.00")

    ledger_result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)
    )
    ledger = list(ledger_result.scalars().all())
    assert sorted((entry.operation_type, Decimal(entry.amount_rub)) for entry in ledger) == sorted(
        [
            ("generation_reserve", Decimal("-80.00")),
            ("generation_reserve_release", Decimal("80.00")),
            ("generation_late_charge", Decimal("-80.00")),
        ]
    )

    coverage_result = await db_session.execute(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
    )
    coverage = list(coverage_result.scalars().all())
    assert sorted((entry.operation_type, Decimal(entry.amount_rub)) for entry in coverage) == sorted(
        [
            ("provider_cost_reserve", Decimal("-20.00")),
            ("provider_cost_reserve_release", Decimal("20.00")),
            ("provider_cost_late_charge", Decimal("-20.00")),
        ]
    )

    media_result = await db_session.execute(
        select(MediaAsset).where(MediaAsset.generation_id == generation.id)
    )
    asset = media_result.scalar_one_or_none()
    assert asset is not None
    assert asset.status == "provider_ready"

    event_result = await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.generation_id == generation.id)
    )
    events = list(event_result.scalars().all())
    assert sorted(event.event_type for event in events) == ["completed", "timeout"]


class _SubmitRetryAfterAdapter(_CountingProviderAdapter):
    async def submit_generation(self, payload):
        from app.providers.base import ProviderAdapterError

        self.submit_calls += 1
        raise ProviderAdapterError(
            "provider_temporarily_unavailable",
            "argolink_rate_limited",
            retryable=True,
            retry_after_seconds=17.0,
        )

    def normalize_error(self, error: Exception):
        return error


class _AmbiguousSubmitAdapter(_CountingProviderAdapter):
    async def submit_generation(self, payload):
        from app.providers.base import ProviderAdapterError

        self.submit_calls += 1
        raise ProviderAdapterError(
            "provider_temporarily_unavailable",
            "argolink_submit_outcome_unknown",
            retryable=False,
        )

    def normalize_error(self, error: Exception):
        return error


class _PollRetryAfterAdapter(_CountingProviderAdapter):
    async def poll_generation(self, provider_task_id: str):
        from app.providers.base import ProviderAdapterError

        raise ProviderAdapterError(
            "provider_temporarily_unavailable",
            "argolink_rate_limited",
            retryable=True,
            retry_after_seconds=23.0,
        )

    def normalize_error(self, error: Exception):
        return error


async def test_submit_retry_after_defers_without_releasing_partner_reserve(db_session, monkeypatch):
    from app.generations.service import dispatch_generation_to_provider
    from app.infrastructure.retry import utc_now

    partner = Partner(
        telegram_id=f"retry-after-submit-{uuid4()}",
        company_name="Retry After Partner",
        project_name="Retry After Bot",
        balance_rub=Decimal("100.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="retry-after-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=f"retry-after-submit-{uuid4()}",
        partner_price_rub=Decimal("80.00"),
        prompt="retry after submit",
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

    adapter = _SubmitRetryAfterAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr("app.generations.service.get_provider_rate_limiter", lambda *args: _NoopRateLimiter())

    before = utc_now()
    attempt = await dispatch_generation_to_provider(db_session, generation)
    after = utc_now()

    assert attempt is not None
    assert adapter.submit_calls == 1
    assert attempt.status == "retry_pending"
    assert generation.status == "queued"
    assert attempt.next_attempt_at is not None
    # SQLite returns UTC columns without tzinfo; timestamp() would interpret
    # them in the machine's local zone (e.g. UTC+3) and fail outside UTC.
    deadline = attempt.next_attempt_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    assert before.timestamp() + 16.5 <= deadline.timestamp()
    assert deadline.timestamp() <= after.timestamp() + 17.5

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("20.00")

    ledger = list(
        (
            await db_session.execute(
                select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(entry.operation_type, Decimal(entry.amount_rub)) for entry in ledger] == [
        ("generation_reserve", Decimal("-80.00"))
    ]


async def test_ambiguous_submit_enters_reconciliation_without_replay_or_refund(db_session, monkeypatch):
    from app.generations.service import dispatch_generation_to_provider

    partner = Partner(
        telegram_id=f"ambiguous-submit-{uuid4()}",
        company_name="Ambiguous Submit Partner",
        project_name="Ambiguous Submit Bot",
        balance_rub=Decimal("100.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="ambiguous-submit-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=f"ambiguous-submit-{uuid4()}",
        partner_price_rub=Decimal("80.00"),
        prompt="ambiguous submit",
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

    adapter = _AmbiguousSubmitAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr("app.generations.service.get_provider_rate_limiter", lambda *args: _NoopRateLimiter())

    first = await dispatch_generation_to_provider(db_session, generation)
    second = await dispatch_generation_to_provider(db_session, generation)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert adapter.submit_calls == 1
    assert generation.status == "reconciliation_required"
    assert generation.public_error_code == "submission_outcome_unknown"
    assert first.status == "reconciliation_required"
    assert first.provider_task_id is None
    assert first.next_attempt_at is None

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("20.00")

    ledger = list(
        (
            await db_session.execute(
                select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(entry.operation_type, Decimal(entry.amount_rub)) for entry in ledger] == [
        ("generation_reserve", Decimal("-80.00"))
    ]


async def test_poll_retry_after_preserves_provider_task_and_defers_next_poll(db_session, monkeypatch):
    from app.generations.service import poll_generation_provider
    from app.infrastructure.retry import utc_now

    partner = Partner(
        telegram_id=f"retry-after-poll-{uuid4()}",
        company_name="Poll Retry Partner",
        project_name="Poll Retry Bot",
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="poll-retry-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=f"retry-after-poll-{uuid4()}",
        partner_price_rub=Decimal("80.00"),
        prompt="retry after poll",
        status="processing",
    )
    db_session.add(generation)
    await db_session.flush()

    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id="existing-provider-task",
        status="processing",
        next_poll_at=utc_now(),
    )
    db_session.add(attempt)
    await db_session.flush()

    adapter = _PollRetryAfterAdapter()

    async def get_adapter(db, partner_id: str, provider: str = "argolink", **kwargs):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    monkeypatch.setattr("app.generations.service.get_provider_rate_limiter", lambda *args: _NoopRateLimiter())

    before = utc_now()
    result = await poll_generation_provider(db_session, generation)
    after = utc_now()

    assert result.status == "processing"
    assert attempt.status == "retry_pending"
    assert attempt.provider_task_id == "existing-provider-task"
    assert attempt.next_attempt_at is not None
    assert before.timestamp() + 22.5 <= attempt.next_attempt_at.timestamp()
    assert attempt.next_attempt_at.timestamp() <= after.timestamp() + 23.5
    assert attempt.retry_count == 1
