from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_native_inference import setup

from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.infrastructure.retry import utc_now
from app.providers.argolink import ArgoLinkAdapter
from app.workers.generation_worker import process_generation_work_concurrently_once


async def admit(client, db_session, monkeypatch, handler):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video",
        rates=[("default", "720p", "second", Decimal("20"), Decimal(".078"))],
    )
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(
        return_value=ArgoLinkAdapter(api_key="fixture", client=upstream),
    ))
    response = await client.post("/v1/videos/generations", json={
        "model": "seedance-2.5", "prompt": "synthetic", "duration": 5, "resolution": "720p",
    }, headers=headers)
    assert response.status_code == 202, response.text
    generation = await db_session.get(Generation, response.json()["request_id"])
    return partner, headers, upstream, generation


async def test_new_admission_enrolls_but_known_acceptance_clears_deadline(client, db_session, monkeypatch):
    async def accepted(request):
        assert generation.client_release_due_at is not None  # Durable before paid submit.
        return httpx.Response(202, json={"request_id": "known-test-id"})

    _, headers, upstream, generation = await admit(client, db_session, monkeypatch, accepted)
    assert generation.client_release_policy == "unconfirmed_video_30m_v1"
    assert generation.client_release_due_at is None
    await dispatch_generation_to_provider(db_session, generation)
    assert generation.client_release_due_at is None
    assert generation.client_reserve_released_at is None
    response = await client.get(f"/v1/videos/{generation.id}", headers=headers)
    assert "financial_status" not in response.json()
    await upstream.aclose()


async def test_ineligible_oldest_rows_cannot_starve_final_paid_release(db_session):
    from test_final_client_release import seed_release

    from app.providers.models import ProviderAttempt
    from app.workers.generation_worker import _release_due_client_reserves

    _, free, _ = await seed_release(db_session, enrolled=True)
    free.partner_price_rub = 0
    free.client_release_due_at = utc_now() - timedelta(hours=2)
    _, competing, _ = await seed_release(db_session, enrolled=True)
    competing.client_release_due_at = utc_now() - timedelta(hours=1)
    db_session.add(ProviderAttempt(generation_id=competing.id, provider="infai", status="retry_pending"))
    owner, paid, _ = await seed_release(db_session, enrolled=True)
    assert await _release_due_client_reserves(db_session, limit=1) == 1
    assert paid.client_reserve_released_at is not None and owner.balance_rub == 1000
    assert free.client_reserve_released_at is None
    assert competing.client_reserve_released_at is None


def test_zero_price_admission_never_enrolls_release_clock():
    from app.billing.client_release import arm_client_release_policy, enroll_client_release_policy

    generation = Generation(partner_price_rub=Decimal("0"))
    enroll_client_release_policy(generation)
    arm_client_release_policy(generation)
    assert generation.client_release_policy is None and generation.client_release_due_at is None


async def test_one_release_failure_is_contained_and_other_jobs_still_finish(db_session, monkeypatch):
    from test_final_client_release import seed_release

    from app.billing import client_release

    _, failed, _ = await seed_release(db_session, enrolled=True)
    _, ready, _ = await seed_release(db_session, enrolled=True)
    original = client_release.release_expired_client_reserve

    async def flaky(db, generation, **kwargs):
        if generation.id == failed.id:
            raise RuntimeError("synthetic transient database failure")
        return await original(db, generation, **kwargs)

    monkeypatch.setattr(client_release, "release_expired_client_reserve", flaky)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    result = await process_generation_work_concurrently_once(session_factory=factory)
    assert result.released == 1

    await db_session.refresh(ready)
    await db_session.refresh(failed)
    assert ready.client_reserve_released_at is not None and failed.client_reserve_released_at is None
    monkeypatch.setattr(client_release, "release_expired_client_reserve", original)
    result = await process_generation_work_concurrently_once(session_factory=factory)
    assert result.released == 1


async def test_crashed_finally_released_intent_stays_visible_for_reconciliation(db_session):
    from test_final_client_release import seed_release

    from app.billing.client_release import release_expired_client_reserve
    from app.inference.admin import pending

    _, generation, attempt = await seed_release(db_session, enrolled=True)
    generation.status = "sent_to_provider"
    attempt.status = "submitting"
    await db_session.commit()
    assert await release_expired_client_reserve(db_session, generation)
    rows = await pending(db_session)
    assert len(rows) == 1 and rows[0]["id"] == generation.id
    assert rows[0]["reserved_rub"] == "0.00"
    assert rows[0]["original_reserved_rub"] == "100.00"
    assert rows[0]["financial_status"] == "released_final"


async def test_restart_worker_final_credit_then_late_result_without_recharge(client, db_session, monkeypatch):
    paid_calls = []

    def unknown(request):
        paid_calls.append(request)
        raise httpx.ReadTimeout("synthetic uncertain acceptance", request=request)

    partner, headers, upstream, generation = await admit(client, db_session, monkeypatch, unknown)
    await dispatch_generation_to_provider(db_session, generation)
    assert generation.status == "reconciliation_required"
    assert generation.client_release_due_at is not None
    generation.client_release_due_at = utc_now() - timedelta(seconds=1)
    from app.providers.models import ProviderAttempt

    original_attempt = await db_session.scalar(select(ProviderAttempt).where(
        ProviderAttempt.generation_id == generation.id,
    ))
    original_attempt.created_at = utc_now() - timedelta(minutes=31)
    await db_session.commit()
    coverage_before = partner.cost_coverage_rub
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    first = await process_generation_work_concurrently_once(session_factory=factory)
    second = await process_generation_work_concurrently_once(session_factory=factory)
    assert first.released == 1 and second.released == 0
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1000000")
    assert partner.cost_coverage_rub == coverage_before
    assert generation.actual_charge_rub is None  # Provider accounting is not settled.
    assert len(paid_calls) == 1
    pending = (await client.get(f"/v1/videos/{generation.id}", headers=headers)).json()
    assert pending["status"] == "pending"
    assert pending["financial_status"] == "released_final" and pending["charged_rub"] == "0.00"
    assert pending["error"]["code"] == "submission_outcome_unknown"

    # Model a later positively recovered provider identity, without submitting again.
    attempt = await db_session.scalar(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation.id))
    attempt.provider_task_id = "recovered-test-id"
    attempt.status = generation.status = "processing"
    attempt.next_poll_at = attempt.next_attempt_at = None
    await db_session.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "request_id": "recovered-test-id", "status": "done", "usage": {"billed_seconds": 5},
        "video": {"url": "https://provider.example/result.mp4"},
    })), base_url="https://argolink.io") as polled:
        monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(
            return_value=ArgoLinkAdapter(api_key="fixture", client=polled),
        ))
        await poll_generation_provider(db_session, generation)
        await poll_generation_provider(db_session, generation)
    assert generation.status == "completed" and generation.actual_charge_rub == 0
    assert generation.actual_provider_cost_usdt == Decimal(".390")
    assert partner.balance_rub == Decimal("1000000")
    assert generation.usage_snapshot == {"seconds": 5}
    entries = list(await db_session.scalars(select(LedgerEntry)))
    assert [item.operation_type for item in entries] == ["generation_reserve", "generation_reserve_release"]
    assert len(list(await db_session.scalars(select(CoverageLedgerEntry)))) == 2
    done = (await client.get(f"/v1/videos/{generation.id}", headers=headers)).json()
    assert done["status"] == "done" and done["video"]["url"]
    assert done["financial_status"] == "released_final" and done["charged_rub"] == "0.00"
    await upstream.aclose()
