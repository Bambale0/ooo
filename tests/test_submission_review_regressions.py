"""Review regressions: a current POST and terminal primary IDs are not live work alike."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.generations.service import dispatch_generation_to_provider
from app.generations.submission_recovery import recover_unconfirmed_submissions_once
from app.infrastructure.retry import utc_now
from app.providers.base import ProviderSubmitResult
from app.providers.models import ProviderAttempt
from tests.test_submission_finality_release import seed


async def test_old_retry_attempt_gets_fresh_submit_deadline_before_provider_post(db_session, monkeypatch):
    _, g, a = await seed(db_session, status="queued")
    a.status = "retry_pending"
    original_created_at = a.created_at
    await db_session.commit()

    async def submit(request):
        assert await recover_unconfirmed_submissions_once(db_session) == 0
        assert g.status == "sent_to_provider" and g.billing_status == "pending"
        assert a.created_at == original_created_at
        return ProviderSubmitResult(provider_task_id="fresh-retry-id", status="accepted", raw_response={})

    def normalize(error):
        raise error

    adapter = SimpleNamespace(submit_generation=submit, normalize_error=normalize)
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr("app.generations.service.get_active_provider_credential", AsyncMock(return_value=None))
    monkeypatch.setattr("app.providers.circuit.admit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.generations.service.get_provider_rate_limiter", lambda *args: SimpleNamespace(acquire=AsyncMock())
    )
    await dispatch_generation_to_provider(db_session, g)
    assert g.status == "sent_to_provider" and a.provider_task_id == "fresh-retry-id"


@pytest.mark.parametrize("primary_status", ["failed", "cancelled"])
async def test_terminal_primary_task_id_does_not_strand_unknown_fallback(db_session, primary_status):
    _, g, fallback = await seed(db_session)
    fallback.provider = "infai"
    prior = ProviderAttempt(
        generation_id=g.id,
        provider="argolink",
        status=primary_status,
        provider_task_id="old-terminal-primary-id",
        cost_status="confirmed_free",
        provider_cost_usdt=Decimal("0"),
        created_at=utc_now() - timedelta(hours=2),
    )
    db_session.add(prior)
    await db_session.commit()
    assert await recover_unconfirmed_submissions_once(db_session) == 1
    assert g.status == "failed" and g.billing_status == "not_chargeable"
    assert prior.cost_status == "confirmed_free" and prior.provider_task_id == "old-terminal-primary-id"
    assert fallback.cost_status == "unknown" and fallback.status == "reconciliation_required"


@pytest.mark.parametrize(
    "primary_status", ["accepted", "processing", "retry_pending", "timeout", "reconciliation_required", "completed"]
)
async def test_primary_id_without_definitive_failure_remains_protected(db_session, primary_status):
    _, g, fallback = await seed(db_session)
    fallback.provider = "infai"
    db_session.add(
        ProviderAttempt(
            generation_id=g.id,
            provider="argolink",
            status=primary_status,
            provider_task_id="protected-primary",
            created_at=utc_now() - timedelta(hours=2),
        )
    )
    await db_session.commit()
    assert await recover_unconfirmed_submissions_once(db_session) == 0
    assert g.status == "reconciliation_required"


@pytest.mark.integration
async def test_postgres_fresh_post_marker_excludes_old_attempt_until_actual_deadline():
    from app.generations.models import Generation
    from tests.test_native_postgres_finality import cleanup, infrastructure
    from tests.test_native_postgres_finality import seed as pg_seed

    engine, factory = infrastructure()
    ids = await pg_seed(factory)
    try:
        async with factory() as db:
            g = await db.get(Generation, ids[1])
            a = await db.get(ProviderAttempt, ids[2])
            g.mode, g.model_slug = "videos/generations", "seedance-2.5"
            g.status, a.status = "sent_to_provider", "submitting"
            g.created_at = a.created_at = utc_now() - timedelta(hours=2)
            g.request_payload = {
                "partner_billing_policy": "actual_only",
                "protocol": "videos/generations",
                "provider_submission_started_at": utc_now().isoformat(),
            }
            await db.commit()
        async with factory() as db:
            assert await recover_unconfirmed_submissions_once(db) == 0
            g = await db.get(Generation, ids[1])
            g.request_payload = {
                **g.request_payload,
                "provider_submission_started_at": (utc_now() - timedelta(hours=1)).isoformat(),
            }
            await db.commit()
        async with factory() as db:
            assert await recover_unconfirmed_submissions_once(db) == 1
            await db.commit()
            g = await db.get(Generation, ids[1])
            assert g.status == "failed" and g.billing_status == "not_chargeable"
    finally:
        await cleanup(factory, ids)
        await engine.dispose()
