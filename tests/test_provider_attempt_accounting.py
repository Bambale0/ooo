from decimal import Decimal

from sqlalchemy import select
from test_infai_fallback import PrimaryFailure, seed

from app.accounts.models import Partner
from app.billing.service import apply_cost_coverage_change
from app.generations.service import poll_generation_provider
from app.infrastructure.retry import utc_now
from app.providers.base import ProviderPollResult
from app.providers.circuit import observe
from app.providers.models import ProviderAttempt, ProviderCircuit, ProviderOutcome


async def test_failed_provider_attempt_persists_reported_usage(db_session, monkeypatch):
    partner, generation, primary, _ = await seed(db_session)
    generation.model_slug = "no-equivalent-fallback"

    class FailedWithUsage(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="failed",
                error_code="provider_failed",
                usage={"billed_seconds": 12, "provider_charge_usdt": "1.75"},
            )

    async def adapter(*args, **kwargs):
        return FailedWithUsage()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)

    await poll_generation_provider(db_session, generation, "argolink")

    assert generation.status == "failed"
    assert primary.usage_snapshot == {"billed_seconds": 12, "provider_charge_usdt": "1.75"}
    assert primary.cost_status == "reported"
    assert primary.provider_cost_usdt == Decimal("1.75")
    assert generation.actual_provider_cost_usdt == Decimal("1.75")
    assert partner.cost_coverage_rub == Decimal("851.25")


async def test_infai_success_releases_confirmed_free_argolink_primary_cost(db_session, monkeypatch):
    partner, generation, primary, credential = await seed(db_session)

    class Infai(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="completed",
                usage={"billed_seconds": 15, "completion_tokens": 324000},
            )

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation, "argolink")
    assert primary.cost_status == "confirmed_free"
    assert primary.provider_cost_usdt == Decimal("0")
    assert primary.cost_reserve_usdt == Decimal("2.5")

    fallback = ProviderAttempt(
        generation_id=generation.id,
        provider="infai",
        credential_id=credential.id,
        provider_task_id="infai-job",
        status="processing",
        next_poll_at=utc_now(),
    )
    db_session.add(fallback)
    generation.status = "processing"
    await db_session.commit()

    await poll_generation_provider(db_session, generation, "infai")

    assert generation.status == "completed"
    assert generation.actual_provider_cost_usdt == Decimal("2.929446")
    assert generation.provider_cost_hold_usdt == Decimal("0")
    assert generation.provider_cost_hold_rub == Decimal("0")
    assert fallback.cost_status == "settled"
    assert fallback.provider_cost_usdt == Decimal("2.929446")
    # ArgoLink's definitive failed status is free; only InfAI's successful
    # attempt is charged to procurement coverage.
    assert partner.cost_coverage_rub == Decimal("751.00")


async def test_provider_outcomes_are_idempotent_per_generation_and_provider(db_session):
    partner = Partner(
        telegram_id="provider-outcome-partner",
        company_name="Outcome",
        project_name="Outcome",
        status="active",
        balance_rub=Decimal("0"),
        cost_coverage_rub=Decimal("0"),
    )
    db_session.add(partner)
    await db_session.flush()
    generation = type("Terminal", (), {"id": "same-generation", "status": "completed", "public_error_code": None})()

    await observe(db_session, generation, provider="argolink", outcome="error")
    await observe(db_session, generation, provider="infai", outcome="success")
    await observe(db_session, generation, provider="infai", outcome="success")

    outcomes = list(
        await db_session.scalars(
            select(ProviderOutcome)
            .where(ProviderOutcome.generation_id == generation.id)
            .order_by(ProviderOutcome.provider)
        )
    )
    assert [(item.provider, item.outcome) for item in outcomes] == [
        ("argolink", "error"),
        ("infai", "success"),
    ]


async def test_infai_terminal_success_releases_infai_recovery_probe(db_session, monkeypatch):
    _, generation, _, credential = await seed(db_session)

    async def primary_adapter(*args, **kwargs):
        return PrimaryFailure()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", primary_adapter)
    await poll_generation_provider(db_session, generation, "argolink")
    db_session.add(
        ProviderAttempt(
            generation_id=generation.id,
            provider="infai",
            credential_id=credential.id,
            provider_task_id="infai-job",
            status="processing",
            next_poll_at=utc_now(),
        )
    )
    db_session.add(
        ProviderCircuit(
            provider="infai",
            state="recovering",
            episode=1,
            healthy_checks=3,
            real_successes=0,
            in_flight=generation.id,
            probe_started_at=utc_now(),
        )
    )
    generation.status = "processing"
    await db_session.commit()

    class Infai(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="completed",
                usage={"billed_seconds": 15, "completion_tokens": 324000},
            )

    async def adapter(*args, **kwargs):
        return Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation, "infai")

    circuit = await db_session.get(ProviderCircuit, "infai")
    assert circuit.in_flight is None
    assert circuit.real_successes == 1
    assert await db_session.get(ProviderOutcome, (generation.id, "infai")) is not None
    assert await db_session.get(ProviderOutcome, (generation.id, "argolink")) is not None


async def test_admin_can_list_and_reconcile_unknown_attempt_cost(client, db_session, admin_headers):
    partner, generation, primary, _ = await seed(db_session)
    generation.status = "completed"
    generation.actual_provider_cost_usdt = Decimal("2.929446")
    generation.provider_cost_hold_usdt = Decimal("2.5")
    generation.provider_cost_hold_rub = Decimal("212.50")
    primary.status = "failed"
    primary.cost_status = "unknown"
    primary.cost_reserve_usdt = Decimal("2.5")
    await apply_cost_coverage_change(
        db_session,
        partner,
        Decimal("27.67"),
        "provider_usage_adjustment",
        f"provider-usage:{generation.id}",
        generation.id,
        allow_negative=True,
    )
    await db_session.commit()

    pending = await client.get("/api/v1/providers/reconciliation", headers=admin_headers)
    assert pending.status_code == 200
    row = next(item for item in pending.json() if item["id"] == generation.id)
    assert row["provider_cost_obligations"] == [
        {
            "attempt_id": primary.id,
            "provider": "argolink",
            "cost_status": "unknown",
            "reserved_usdt": "2.5",
        }
    ]

    reconciled = await client.post(
        f"/api/v1/providers/reconciliation/{generation.id}/attempts/{primary.id}/cost",
        headers=admin_headers,
        json={"outcome": "free", "reason": "Provider confirmed that the failed task was not billed."},
    )
    assert reconciled.status_code == 200, reconciled.text
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    await db_session.refresh(primary)
    assert primary.cost_status == "confirmed_free"
    assert primary.provider_cost_usdt == Decimal("0")
    assert generation.provider_cost_hold_usdt == Decimal("0")
    assert generation.provider_cost_hold_rub == Decimal("0")
    assert generation.actual_provider_cost_usdt == Decimal("2.929446")
    assert partner.cost_coverage_rub == Decimal("751.00")


async def test_migrated_unknown_cost_reconciliation_does_not_release_coverage_twice(client, db_session, admin_headers):
    partner, generation, primary, _ = await seed(db_session)
    generation.status = "completed"
    generation.actual_provider_cost_usdt = Decimal("2.929446")
    generation.provider_cost_hold_usdt = Decimal("2.5")
    generation.provider_cost_hold_rub = Decimal("212.50")
    primary.status = "failed"
    primary.cost_status = "unknown"
    primary.cost_reserve_usdt = Decimal("2.5")
    # Legacy settlement already retained only the known InfAI charge. Migration
    # adds the unresolved hold fields but must not invent a second coverage reserve.
    await apply_cost_coverage_change(
        db_session,
        partner,
        Decimal("240.17"),
        "legacy_provider_usage_adjustment",
        f"legacy-provider-usage:{generation.id}",
        generation.id,
        allow_negative=True,
    )
    before = partner.cost_coverage_rub
    await db_session.commit()

    response = await client.post(
        f"/api/v1/providers/reconciliation/{generation.id}/attempts/{primary.id}/cost",
        headers=admin_headers,
        json={"outcome": "free", "reason": "Provider confirmed the historical failed task was free."},
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(partner)
    await db_session.refresh(generation)
    assert partner.cost_coverage_rub == before
    assert generation.provider_cost_hold_usdt == Decimal("0")

    replay = await client.post(
        f"/api/v1/providers/reconciliation/{generation.id}/attempts/{primary.id}/cost",
        headers=admin_headers,
        json={"outcome": "free", "reason": "Provider confirmed that the failed task was not billed."},
    )
    assert replay.status_code == 200
    await db_session.refresh(partner)
    assert partner.cost_coverage_rub == Decimal("751.00")


async def test_legacy_reconciliation_selects_latest_provider_attempt(client, db_session, admin_headers):
    _, generation, primary, credential = await seed(db_session)
    primary.status = "failed"
    fallback = ProviderAttempt(
        generation_id=generation.id,
        provider="infai",
        credential_id=credential.id,
        provider_task_id="latest-fallback-task",
        status="reconciliation_required",
        created_at=utc_now(),
    )
    db_session.add(fallback)
    generation.status = "reconciliation_required"
    await db_session.commit()

    response = await client.post(
        f"/api/v1/providers/reconciliation/{generation.id}",
        headers=admin_headers,
        json={"outcome": "not_accepted", "reason": "Provider confirmed the latest task was not accepted."},
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(primary)
    await db_session.refresh(fallback)
    assert primary.status == "failed"
    assert fallback.status == "failed"
