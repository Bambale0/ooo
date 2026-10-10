"""A confirmed ArgoLink failed video job is free, including procurement coverage."""

from decimal import Decimal

from sqlalchemy import select
from test_infai_fallback import PrimaryFailure, seed
from test_telegram_cabinet import cabinet as cabinet_fixture
from test_telegram_cabinet import partner as make_partner

from app.billing.models import CoverageLedgerEntry
from app.generations.models import Generation
from app.generations.service import poll_generation_provider

cabinet = cabinet_fixture


async def test_argolink_terminal_failed_video_releases_both_reserves_once(db_session, monkeypatch):
    partner, generation, attempt, _ = await seed(db_session)
    generation.model_slug = "no-equivalent-fallback"

    async def get_adapter(*args, **kwargs):
        return PrimaryFailure()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    await poll_generation_provider(db_session, generation, "argolink")
    assert generation.status == attempt.status == "failed"
    assert attempt.cost_status == "confirmed_free"
    assert attempt.provider_cost_usdt == Decimal("0")
    assert generation.actual_provider_cost_usdt == Decimal("0")
    assert generation.provider_cost_hold_usdt == Decimal("0")
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1000")
    assert partner.cost_coverage_rub == Decimal("1000")
    ledger = list(
        await db_session.scalars(select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id))
    )
    assert sum((x.amount_rub for x in ledger), Decimal(0)) == 0
    count = len(ledger)
    await poll_generation_provider(db_session, generation, "argolink")
    assert (
        len(
            list(
                await db_session.scalars(
                    select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
                )
            )
        )
        == count
    )


async def test_admin_generation_summary_shows_actual_zero_on_failed_not_quote(cabinet, db_session):
    feed, _ = cabinet
    owner = await make_partner(db_session)
    generation = Generation(
        partner_id=owner.id,
        model_id="00000000-0000-0000-0000-000000000111",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=41,
        idempotency_key="failed-reserve-admin-20261010",
        partner_price_rub=Decimal("781.54"),
        prompt="",
        status="failed",
    )
    db_session.add(generation)
    await db_session.commit()
    view = (await feed(user=999, callback=f"admin_partner_gens:{owner.id}"))[-1].text
    assert generation.id in view
    assert "Списано: 0.00 ₽" in view
    assert "781.54" not in view
    partner_view = (await feed(user=int(owner.telegram_id), callback="history:0"))[-1].text
    assert "781.54" not in partner_view
