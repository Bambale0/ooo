"""Admission must not confuse historical procurement risk with live funding."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.capital import capital_state, provider_capital_state, require_provider_capital
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt


async def seed_hold(db, *, amount="160.06363", age=3600, status="failed", provider="argolink", cost_status="unknown"):
    partner = Partner(telegram_id=str(uuid4()), company_name="Capital regression", project_name="Isolated")
    db.add(partner)
    await db.flush()
    g = Generation(
        partner_id=partner.id,
        model_id="fixture",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="480p",
        status=status,
        idempotency_key=str(uuid4()),
        partner_price_rub=Decimal("20"),
        prompt="test",
        provider_cost_usdt_snapshot=Decimal(amount),
        provider_cost_hold_usdt=Decimal(amount),
        created_at=utc_now() - timedelta(seconds=age),
    )
    db.add(g)
    await db.flush()
    a = ProviderAttempt(
        generation_id=g.id,
        provider=provider,
        status="reconciliation_required",
        cost_status=cost_status,
        cost_reserve_usdt=Decimal(amount),
        created_at=g.created_at,
    )
    db.add(a)
    await db.flush()
    return partner, g, a


@pytest.fixture
def funding(monkeypatch):
    read_balance = AsyncMock(return_value=Decimal("57.94033829"))
    adapter = SimpleNamespace(prepaid_balance_usdt=read_balance)
    monkeypatch.setattr("app.billing.capital.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr(get_settings(), "worker_provider_processing_timeout_seconds", 1800)
    monkeypatch.setattr(get_settings(), "required_provider_float_usdt", Decimal(0))
    return read_balance


@pytest.mark.parametrize("status", ["failed", "completed", "cancelled"])
async def test_historical_holds_do_not_block_new_work_or_disappear_from_risk(db_session, funding, status):
    partner, g, a = await seed_hold(db_session, age=7 * 86400, status=status)
    before = await capital_state(db_session)
    state = await provider_capital_state(db_session, partner.id)
    assert state["available"] == Decimal("57.94033829")
    await require_provider_capital(db_session, Decimal("1"), partner_id=partner.id)
    after = await capital_state(db_session)
    assert after["safe"] == before["safe"]
    assert after["components"]["unresolved_provider_cost_hold_usdt"] == Decimal("160.06363")
    assert g.provider_cost_hold_usdt == a.cost_reserve_usdt == Decimal("160.06363")
    for table in (LedgerEntry, CoverageLedgerEntry):
        assert not list(await db_session.scalars(select(table)))


@pytest.mark.parametrize("status", ["failed", "completed", "cancelled"])
async def test_recent_unknown_attempt_still_blocks_overcommit(db_session, funding, status):
    partner, _, _ = await seed_hold(db_session, age=60, amount="57", status=status)
    state = await provider_capital_state(db_session, partner.id)
    assert state["active"] == Decimal("57")
    with pytest.raises(HTTPException) as exc:
        await require_provider_capital(db_session, Decimal("1"), partner_id=partner.id)
    assert exc.value.status_code == 503
    await require_provider_capital(db_session, Decimal(".94"), partner_id=partner.id)


async def test_other_provider_terminal_obligation_does_not_consume_argolink(db_session, funding):
    partner, _, _ = await seed_hold(db_session, age=60, provider="infai")
    assert (await provider_capital_state(db_session, partner.id, "argolink"))["active"] == 0
    assert (await provider_capital_state(db_session, partner.id, "infai"))["active"] == Decimal("160.06363")


async def test_recent_shared_provider_hold_is_not_mistaken_for_independent_partner_wallet(db_session, funding):
    partner, _, _ = await seed_hold(db_session, age=60, amount="57")
    other = Partner(telegram_id=str(uuid4()), company_name="Other", project_name="Other")
    db_session.add(other)
    await db_session.flush()
    state = await provider_capital_state(db_session, other.id)
    assert state["active"] == Decimal("57")
    assert other.id != partner.id


@pytest.mark.parametrize("cost_status", ["settled", "confirmed_free"])
async def test_resolved_attempt_does_not_reuse_stale_hold(db_session, funding, cost_status):
    partner, _, _ = await seed_hold(db_session, age=60, cost_status=cost_status)
    assert (await provider_capital_state(db_session, partner.id))["active"] == 0


async def test_attempt_reserve_not_multiplied_by_generation_total(db_session, funding):
    partner, g, a = await seed_hold(db_session, age=60, amount="3")
    g.provider_cost_hold_usdt = Decimal("12")
    db_session.add(
        ProviderAttempt(
            generation_id=g.id,
            provider="infai",
            status="failed",
            cost_status="unknown",
            cost_reserve_usdt=Decimal("9"),
            created_at=a.created_at,
        )
    )
    await db_session.flush()
    assert (await provider_capital_state(db_session, partner.id))["active"] == Decimal("3")


async def test_old_generation_with_recent_attempt_keeps_recent_exposure(db_session, funding):
    partner, _, a = await seed_hold(db_session, age=7 * 86400, amount="57")
    a.created_at = utc_now()
    await db_session.flush()
    assert (await provider_capital_state(db_session, partner.id))["active"] == Decimal("57")


async def test_active_generation_reserve_is_not_counted_twice(db_session, funding):
    partner, _, _ = await seed_hold(db_session, age=60, amount="3", status="processing")
    assert (await provider_capital_state(db_session, partner.id))["active"] == Decimal("3")


async def test_zero_upstream_funds_still_fail_closed(db_session, funding):
    partner, _, _ = await seed_hold(db_session, age=7 * 86400)
    funding.return_value = Decimal(0)
    with pytest.raises(HTTPException) as exc:
        await require_provider_capital(db_session, Decimal(".01"), partner_id=partner.id)
    assert exc.value.status_code == 503


@pytest.mark.parametrize("age,expected", [(1799, "3"), (1800, "3"), (1801, "0")])
async def test_recent_hold_uses_configured_window_boundary(db_session, funding, monkeypatch, age, expected):
    now = utc_now()
    monkeypatch.setattr("app.billing.capital.utc_now", lambda: now)
    partner, _, a = await seed_hold(db_session, amount="3")
    a.created_at = now - timedelta(seconds=age)
    await db_session.flush()
    assert (await provider_capital_state(db_session, partner.id))["active"] == Decimal(expected)


@pytest.mark.parametrize("native", [False, True], ids=["generation-api", "native-video-api"])
async def test_seedance_admission_with_historical_holds_is_idempotent(client, db_session, monkeypatch, funding, native):
    from test_cost_coverage import _seed_generation_preflight
    from test_native_inference import setup as setup_native

    await seed_hold(db_session, age=7 * 86400)
    upstream = None
    if native:

        def no_submission(request):
            raise AssertionError("Admission must not submit paid provider work")

        partner, headers, upstream = await setup_native(
            db_session,
            monkeypatch,
            no_submission,
            model="seedance-2.5",
            category="video",
            rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
        )
        url = "/v1/videos/generations"
        body = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
    else:
        partner_id, token = await _seed_generation_preflight(db_session)
        partner = await db_session.get(Partner, partner_id)
        headers = {"Authorization": f"Bearer {token}"}
        url = "/api/v1/generations"
        body = {
            "model_slug": "seedance-2.5",
            "mode": "text_to_video",
            "resolution": "720p",
            "duration_seconds": 5,
            "prompt": "test",
            "idempotency_key": "historical-risk-retry",
        }
    await db_session.commit()
    original_balance = partner.balance_rub
    try:
        funding.return_value = Decimal(0)
        assert (await client.post(url, headers=headers, json=body)).status_code == 503
        funding.return_value = Decimal("57.94033829")
        accepted = await client.post(url, headers=headers, json=body)
        assert accepted.status_code == 202, accepted.text
        again = await client.post(url, headers=headers, json=body)
        assert again.status_code == 202 and again.json() == accepted.json()
        rows = list(await db_session.scalars(select(Generation).where(Generation.partner_id == partner.id)))
        assert len(rows) == 1 and rows[0].status == "queued"
        assert rows[0].actual_charge_rub is None
        await db_session.refresh(partner)
        assert partner.balance_rub == original_balance
        assert not list(await db_session.scalars(select(LedgerEntry)))
    finally:
        if upstream is not None:
            await upstream.aclose()


@pytest.mark.integration
async def test_postgres_terminal_exposure_does_not_poison_admission(funding):
    import os

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL URL not configured")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    partner_id = None
    try:
        async with factory() as db:
            partner, g, attempt = await seed_hold(db, age=7 * 86400)
            partner_id = partner.id
            await db.commit()
            state = await provider_capital_state(db, partner_id)
            assert state["available"] == Decimal("57.94033829")
            await require_provider_capital(db, Decimal("1"), partner_id=partner_id)
            attempt.created_at = utc_now()
            await db.commit()
            with pytest.raises(HTTPException):
                await require_provider_capital(db, Decimal("1"), partner_id=partner_id)
            await db.rollback()
    finally:
        if partner_id is not None:
            async with factory() as db:
                ids = select(Generation.id).where(Generation.partner_id == partner_id)
                await db.execute(delete(ProviderAttempt).where(ProviderAttempt.generation_id.in_(ids)))
                await db.execute(delete(Generation).where(Generation.partner_id == partner_id))
                await db.execute(delete(Partner).where(Partner.id == partner_id))
                await db.commit()
        await engine.dispose()
