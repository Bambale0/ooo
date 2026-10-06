from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import Partner, ProfitWithdrawal
from app.billing.capital import capital_state, provider_capital_state, require_current_capital
from app.billing.capital import wallet_balance as actual_wallet_balance
from app.billing.models import WalletSnapshot
from app.billing.safe_to_withdraw import record_profit_withdrawal
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayError


async def test_current_reserve_uses_worst_cost_ratio_and_wallet_cap(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal("100"))

    async def wallet(db):
        return Decimal("90"), 0, "fresh"

    monkeypatch.setattr("app.billing.capital.wallet_balance", wallet)
    p = Partner(
        telegram_id="treasury",
        company_name="test",
        project_name="test",
        balance_rub=Decimal("1000"),
        cost_coverage_rub=Decimal("-200"),
    )
    m = Model(slug="test-model", name="test", modality="video", status="production")
    db_session.add_all([p, m])
    await db_session.flush()
    db_session.add(
        PartnerPrice(
            model_id=m.id, mode="default", resolution="480p", price_rub=Decimal("10"), provider_cost_usdt=Decimal(".05")
        )
    )
    db_session.add(
        PartnerPrice(
            model_id=m.id, mode="default", resolution="720p", price_rub=Decimal("10"), provider_cost_usdt=Decimal(".08")
        )
    )
    await db_session.flush()
    state = await capital_state(db_session)
    assert state["safe"] == Decimal("82")
    assert state["components"]["current_future_cost_reserve_usdt"] == Decimal("8")
    await require_current_capital(db_session, Decimal("1"))  # Historical -200 RUB does not deny admission.
    with pytest.raises(HTTPException):
        await require_current_capital(db_session, Decimal("91"))


async def test_wallet_stale_fallback_is_visible_and_cannot_fund_new_requests(db_session, monkeypatch):
    db_session.add(WalletSnapshot(available_usdt=Decimal("17.12"), created_at=utc_now() - timedelta(hours=2)))
    await db_session.flush()

    class Down:
        async def get_available_usdt(self):
            raise CryptoPayError("unavailable")

    monkeypatch.setattr("app.billing.capital.get_crypto_pay_client", lambda: Down())
    monkeypatch.setattr("app.billing.capital.wallet_balance", actual_wallet_balance)
    state = await capital_state(db_session)
    assert state["freshness"] == "stale" and state["wallet_age_seconds"] >= 7200
    assert state["safe"] == Decimal("17.12")
    with pytest.raises(HTTPException):
        await require_current_capital(db_session, Decimal(".01"))


async def test_terminal_unknown_provider_cost_hold_reduces_safe_to_withdraw(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal("100"))

    async def wallet(db):
        return Decimal("100"), 0, "fresh"

    monkeypatch.setattr("app.billing.capital.wallet_balance", wallet)
    partner = Partner(telegram_id="held-cost", company_name="Held", project_name="Held")
    db_session.add(partner)
    await db_session.flush()
    db_session.add(
        Generation(
            partner_id=partner.id,
            model_id="held-cost-model",
            model_slug="seedance-2.5",
            mode="videos/generations",
            resolution="720p",
            status="completed",
            idempotency_key="held-cost",
            partner_price_rub=Decimal("100"),
            provider_cost_usdt_snapshot=Decimal("2"),
            provider_cost_reserve_usdt=Decimal("5"),
            provider_cost_reserve_rub=Decimal("425"),
            provider_cost_hold_usdt=Decimal("2"),
            provider_cost_hold_rub=Decimal("170"),
            actual_provider_cost_usdt=Decimal("3"),
            prompt="test",
        )
    )
    await db_session.flush()

    state = await capital_state(db_session)
    assert state["safe"] == Decimal("95")
    assert state["components"]["unresolved_provider_cost_hold_usdt"] == Decimal("2")


async def test_withdrawals_idempotent_override_and_partial_compensations(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal("5"))
    p = Partner(telegram_id="owner", company_name="owner", project_name="owner")
    db_session.add(p)
    await db_session.flush()
    args = dict(
        partner_id=p.id,
        amount_usdt=Decimal("10"),
        reason="Recorded off-platform withdrawal",
        idempotency_key="withdrawal-001",
    )
    with pytest.raises(HTTPException):
        await record_profit_withdrawal(db_session, **args)
    a = await record_profit_withdrawal(db_session, **args, override_reason="Owner accepts deficit risk")
    b = await record_profit_withdrawal(db_session, **args, override_reason="Owner accepts deficit risk")
    assert a["id"] == b["id"]
    assert (await capital_state(db_session))["safe"] == Decimal("-5")
    await record_profit_withdrawal(
        db_session,
        partner_id=p.id,
        amount_usdt=Decimal("-3"),
        reason="Correct actual withdrawn amount",
        idempotency_key="correction-001",
        correction_for_id=a["id"],
    )
    assert (await capital_state(db_session))["safe"] == Decimal("-2")
    with pytest.raises(HTTPException):
        await record_profit_withdrawal(
            db_session,
            partner_id=p.id,
            amount_usdt=Decimal("-8"),
            reason="Cannot reverse beyond original",
            idempotency_key="correction-002",
            correction_for_id=a["id"],
        )
    assert len(list((await db_session.execute(select(ProfitWithdrawal))).scalars())) == 2


@pytest.mark.integration
async def test_postgres_serializes_cash_reservations_across_partners(monkeypatch):
    import asyncio
    import os
    from uuid import uuid4

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.billing.capital import lock_capital
    from app.generations.models import Generation

    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL URL not configured")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal("1"))
    ids = []
    async with factory() as db:
        for _ in range(2):
            p = Partner(telegram_id=str(uuid4()), company_name="cash race", project_name="test")
            db.add(p)
            await db.flush()
            ids.append(p.id)
        await db.commit()

    async def claim(partner_id):
        async with factory() as db:
            try:
                await lock_capital(db)
                await require_current_capital(db, Decimal(".6"))
                db.add(
                    Generation(
                        partner_id=partner_id,
                        model_id="cash-race",
                        model_slug="seedance-2.5",
                        mode="default",
                        resolution="480p",
                        status="queued",
                        idempotency_key=str(uuid4()),
                        partner_price_rub=Decimal("120"),
                        provider_cost_usdt_snapshot=Decimal(".6"),
                        prompt="test",
                    )
                )
                await db.commit()
                return True
            except HTTPException:
                await db.rollback()
                return False

    try:
        result = await asyncio.gather(*(claim(pid) for pid in ids))
        assert sorted(result) == [False, True]
    finally:
        async with factory() as db:
            await db.execute(delete(Generation).where(Generation.partner_id.in_(ids)))
            await db.execute(delete(Partner).where(Partner.id.in_(ids)))
            await db.commit()
        await engine.dispose()


async def test_provider_capital_expires_stale_uncertain_reserves(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "required_provider_float_usdt", Decimal("0"))
    monkeypatch.setattr(settings, "worker_provider_processing_timeout_seconds", 30 * 60)

    partner = Partner(telegram_id="provider-capital", company_name="Provider Capital", project_name="Provider Capital")
    db_session.add(partner)
    await db_session.flush()

    async def prepaid_balance():
        return Decimal("10")

    class Adapter:
        async def prepaid_balance_usdt(self):
            return await prepaid_balance()

    async def get_adapter(db, partner_id, provider):
        return Adapter()

    monkeypatch.setattr("app.billing.capital.get_partner_provider_adapter", get_adapter)

    def generation(status, reserve, age_minutes, suffix):
        return Generation(
            partner_id=partner.id,
            model_id=f"provider-capital-{suffix}",
            model_slug="seedance-2.5",
            mode="default",
            resolution="480p",
            status=status,
            idempotency_key=f"provider-capital-{suffix}",
            partner_price_rub=Decimal("100"),
            provider_cost_usdt_snapshot=Decimal(str(reserve)),
            provider_cost_reserve_usdt=Decimal(str(reserve)),
            prompt="test",
            created_at=utc_now() - timedelta(minutes=age_minutes),
        )

    db_session.add_all(
        [
            generation("processing", "3", 5, "processing"),
            generation("reconciliation_required", "2", 5, "fresh-reconciliation"),
            generation("reconciliation_required", "7", 120, "stale-reconciliation"),
            generation("timeout", "5", 120, "stale-timeout"),
        ]
    )
    await db_session.flush()

    state = await provider_capital_state(db_session, partner.id)
    assert state["balance"] == Decimal("10")
    assert state["active"] == Decimal("5")
    assert state["available"] == Decimal("5")
