"""Real PostgreSQL serializes concurrent changes and settlement on the partner row."""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner, PartnerCreditLimitHistory
from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change, set_partner_credit_limit


@pytest.mark.integration
async def test_concurrent_limit_changes_and_charge_preserve_cash_and_single_audit():
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(url, pool_size=8, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    partner_id = None
    try:
        async with factory() as db:
            owner = Partner(
                telegram_id="credit-pg-" + str(uuid4()), company_name="Test", project_name="Credit",
                balance_rub=Decimal("284.70"),
            )
            db.add(owner)
            await db.commit()
            partner_id = owner.id

        async def update():
            async with factory() as db:
                await set_partner_credit_limit(db, partner_id, Decimal(5000), actor="test", reason="Concurrent credit")
                await db.commit()

        async def charge():
            async with factory() as db:
                owner = await db.get(Partner, partner_id)
                await apply_partner_balance_change(
                    db, owner, Decimal("-10"), "manual_adjustment", "credit-pg-charge:" + partner_id
                )
                await db.commit()

        await asyncio.wait_for(asyncio.gather(*(update() for _ in range(6)), charge(), charge()), timeout=20)
        async with factory() as db:
            owner = await db.get(Partner, partner_id)
            assert owner.balance_rub == Decimal("274.70")
            assert owner.credit_limit_rub == Decimal(5000)
            assert owner.available_balance_rub == Decimal("5274.70")
            count = await db.scalar(
                select(func.count()).select_from(PartnerCreditLimitHistory)
                .where(PartnerCreditLimitHistory.partner_id == partner_id)
            )
            assert count == 1
            ledger_count = await db.scalar(
                select(func.count()).select_from(LedgerEntry).where(LedgerEntry.partner_id == partner_id)
            )
            assert ledger_count == 1
    finally:
        if partner_id:
            async with factory() as db:
                await db.execute(
                    delete(PartnerCreditLimitHistory).where(PartnerCreditLimitHistory.partner_id == partner_id)
                )
                await db.execute(delete(LedgerEntry).where(LedgerEntry.partner_id == partner_id))
                await db.execute(delete(Partner).where(Partner.id == partner_id))
                await db.commit()
        await engine.dispose()
