from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import (
    apply_cost_coverage_change,
    apply_partner_balance_change,
    release_generation_reserve,
)
from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt


async def seed_release(db, *, enrolled=False):
    partner = Partner(
        telegram_id=f"final-release-{uuid4()}", company_name="Final release", project_name="Final release",
        status="active", balance_rub=Decimal("1000.00"), cost_coverage_rub=Decimal("1000.00"),
    )
    db.add(partner)
    await db.flush()
    generation = Generation(
        partner_id=partner.id, model_id="video", model_slug="seedance-2.0", mode="videos/generations",
        resolution="720p", duration_seconds=10, idempotency_key=str(uuid4()), prompt="local test",
        status="reconciliation_required", partner_price_rub=Decimal("100.00"),
        provider_cost_usdt_snapshot=Decimal("3"), provider_cost_reserve_usdt=Decimal("3"),
        provider_cost_reserve_rub=Decimal("60.00"), rub_per_usdt_snapshot=Decimal("20"),
        public_error_code="submission_outcome_unknown",
        request_payload={"protocol": "videos/generations", "rates": {"seconds": {"retail": "10", "cost": ".5"}}},
    )
    if enrolled:
        from app.billing.client_release import arm_client_release_policy, enroll_client_release_policy

        enroll_client_release_policy(generation)
        arm_client_release_policy(generation, now=utc_now() - timedelta(minutes=31))
    db.add(generation)
    await db.flush()
    attempt = ProviderAttempt(
        generation_id=generation.id, provider="argolink", status="reconciliation_required",
        cost_reserve_usdt=Decimal("3"),
    )
    db.add(attempt)
    await apply_partner_balance_change(
        db, partner, Decimal("-100"), "generation_reserve", f"generation-reserve:{generation.id}", generation.id,
    )
    await apply_cost_coverage_change(
        db, partner, Decimal("-60"), "provider_cost_reserve", f"provider-cost-reserve:{generation.id}", generation.id,
    )
    await db.commit()
    return partner, generation, attempt


async def test_final_release_settles_provider_usage_without_client_recharge(db_session):
    partner, generation, _ = await seed_release(db_session)
    await release_generation_reserve(db_session, generation, reason="Final client reserve release")
    generation.client_reserve_released_at = utc_now()
    await db_session.commit()

    await settle_actual(db_session, generation, {"seconds": 3}, provider_cost_hold_rub=Decimal("20"))
    await settle_actual(db_session, generation, {"seconds": 3}, provider_cost_hold_rub=Decimal("20"))

    assert partner.balance_rub == Decimal("1000.00")
    assert partner.cost_coverage_rub == Decimal("950.00")
    assert generation.actual_charge_rub == Decimal("0.00")
    assert generation.actual_provider_cost_usdt == Decimal("1.5")
    assert generation.usage_snapshot == {"seconds": 3}
    ledger = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
    assert [entry.operation_type for entry in ledger] == ["generation_reserve", "generation_reserve_release"]
    coverage = list(await db_session.scalars(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
    ))
    assert len(coverage) == 2


async def test_expired_release_is_client_only_durable_and_idempotent(db_session):
    from app.billing.capital import capital_state
    from app.billing.client_release import release_expired_client_reserve

    partner, generation, attempt = await seed_release(db_session, enrolled=True)
    generation.provider_cost_hold_usdt = Decimal("1")
    generation.provider_cost_hold_rub = Decimal("20")
    before = await capital_state(db_session)
    before_attempt = (attempt.status, attempt.provider_task_id, attempt.cost_reserve_usdt)
    assert await release_expired_client_reserve(db_session, generation)
    await db_session.commit()
    generation_id = generation.id
    db_session.expire_all()
    generation = await db_session.get(Generation, generation_id)
    partner = await db_session.get(Partner, generation.partner_id)
    attempt = await db_session.scalar(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation.id))
    assert not await release_expired_client_reserve(db_session, generation)

    assert partner.balance_rub == Decimal("1000.00")
    assert partner.cost_coverage_rub == Decimal("940.00")
    assert generation.client_reserve_released_at is not None
    assert generation.client_release_due_at is None
    assert generation.actual_charge_rub is None
    assert generation.actual_provider_cost_usdt is None
    assert generation.status == "reconciliation_required"
    assert generation.public_error_code == "submission_outcome_unknown"
    assert generation.provider_cost_hold_usdt == Decimal("1")
    assert generation.provider_cost_hold_rub == Decimal("20")
    assert (attempt.status, attempt.provider_task_id, attempt.cost_reserve_usdt) == before_attempt
    after = await capital_state(db_session)
    assert (after["components"]["active_generation_reserve_usdt"]
            == before["components"]["active_generation_reserve_usdt"])
    assert await db_session.scalar(select(CoverageLedgerEntry.operation_type).where(
        CoverageLedgerEntry.generation_id == generation.id
    )) == "provider_cost_reserve"


async def test_deadline_boundary_and_crashed_submit_without_error(db_session):
    from app.billing.client_release import release_expired_client_reserve

    _, generation, attempt = await seed_release(db_session, enrolled=True)
    deadline = utc_now()
    generation.client_release_due_at = deadline
    generation.status = "sent_to_provider"
    generation.public_error_code = None
    attempt.status = "submitting"
    await db_session.flush()
    assert not await release_expired_client_reserve(db_session, generation, now=deadline - timedelta(microseconds=1))
    assert await release_expired_client_reserve(db_session, generation, now=deadline)


async def test_legacy_unknown_submission_never_auto_enrolls_or_refunds(db_session):
    from app.billing.client_release import arm_client_release_policy, release_expired_client_reserve

    partner, generation, _ = await seed_release(db_session)
    arm_client_release_policy(generation, now=utc_now() - timedelta(days=1))
    assert not await release_expired_client_reserve(db_session, generation)
    assert generation.client_release_policy is None
    assert generation.client_release_due_at is None
    assert partner.balance_rub == Decimal("900.00")


def test_submit_clock_survives_retry_and_only_rearms_after_definitive_outcome():
    from app.billing.client_release import (
        CLIENT_RELEASE_DELAY,
        arm_client_release_policy,
        clear_client_release_deadline,
        enroll_client_release_policy,
    )

    generation = Generation(partner_price_rub=Decimal("1.00"))
    now = utc_now()
    enroll_client_release_policy(generation)
    assert generation.client_release_due_at is None
    arm_client_release_policy(generation, now=now)
    arm_client_release_policy(generation, now=now + timedelta(minutes=20))
    assert generation.client_release_due_at == now + CLIENT_RELEASE_DELAY
    clear_client_release_deadline(generation)
    arm_client_release_policy(generation, now=now + timedelta(hours=1))
    assert generation.client_release_due_at == now + timedelta(hours=1) + CLIENT_RELEASE_DELAY
    generation.client_reserve_released_at = now
    clear_client_release_deadline(generation)
    arm_client_release_policy(generation, now=now)
    assert generation.client_release_due_at is None


async def test_known_provider_task_does_not_qualify_even_with_stale_deadline(db_session):
    from app.billing.client_release import release_expired_client_reserve

    partner, generation, attempt = await seed_release(db_session, enrolled=True)
    attempt.provider_task_id = "confirmed-task"
    await db_session.flush()
    assert not await release_expired_client_reserve(db_session, generation)
    assert partner.balance_rub == Decimal("900.00")


async def test_newer_retry_or_fallback_blocks_final_release(db_session):
    from app.billing.client_release import release_expired_client_reserve

    _, generation, _ = await seed_release(db_session, enrolled=True)
    db_session.add(ProviderAttempt(generation_id=generation.id, provider="infai", status="retry_pending"))
    await db_session.flush()
    assert not await release_expired_client_reserve(db_session, generation)


async def test_settled_or_terminal_generation_never_gets_an_extra_refund(db_session):
    from app.billing.client_release import release_expired_client_reserve

    _, generation, _ = await seed_release(db_session, enrolled=True)
    generation.actual_charge_rub = Decimal("30")
    await db_session.flush()
    assert not await release_expired_client_reserve(db_session, generation)
    generation.actual_charge_rub = None
    for status in ("completed", "failed", "cancelled", "timeout", "queued", "processing"):
        generation.status = status
        await db_session.flush()
        assert not await release_expired_client_reserve(db_session, generation)


async def test_final_release_then_late_success_settles_explicit_provider_cost_once(db_session):
    from app.billing.client_release import release_expired_client_reserve

    partner, generation, _ = await seed_release(db_session, enrolled=True)
    assert await release_expired_client_reserve(db_session, generation)
    await db_session.commit()
    await settle_actual(db_session, generation, {"seconds": 3}, provider_cost=Decimal("4.75"))
    await db_session.commit()
    await settle_actual(db_session, generation, {"seconds": 3}, provider_cost=Decimal("4.75"))
    assert partner.balance_rub == Decimal("1000.00")
    assert partner.cost_coverage_rub == Decimal("905.00")
    assert generation.actual_charge_rub == Decimal("0.00")
    assert generation.actual_provider_cost_usdt == Decimal("4.75")
    assert generation.usage_snapshot == {"seconds": 3}


async def test_existing_provisional_timeout_keeps_late_charge_behavior(db_session):
    from app.billing.service import release_generation_reserves

    partner, generation, _ = await seed_release(db_session)
    await release_generation_reserves(db_session, generation, reason="Legacy timeout")
    await settle_actual(db_session, generation, {"seconds": 3})
    await settle_actual(db_session, generation, {"seconds": 3})
    assert partner.balance_rub == Decimal("970.00")
    assert partner.cost_coverage_rub == Decimal("970.00")
    assert generation.actual_charge_rub == Decimal("30.00")
    assert generation.actual_provider_cost_usdt == Decimal("1.5")
    assert generation.client_reserve_released_at is None


async def test_terminal_failure_after_final_release_cannot_duplicate_client_credit(db_session):
    from app.billing.client_release import release_expired_client_reserve
    from app.billing.service import release_generation_reserves

    partner, generation, _ = await seed_release(db_session, enrolled=True)
    assert await release_expired_client_reserve(db_session, generation)
    await release_generation_reserves(db_session, generation, reason="Definitive provider failure")
    await release_generation_reserves(db_session, generation, reason="Definitive provider failure replay")
    assert partner.balance_rub == Decimal("1000.00")
    assert partner.cost_coverage_rub == Decimal("1000.00")
    assert generation.client_reserve_released_at is not None
