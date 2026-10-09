from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.billing.capital import capital_state


async def test_supplier_credit_is_idempotent_and_never_withdrawable(db_session):
    from app.billing.provider_credits import record_supplier_credit
    from app.providers.models import ProviderCredit

    before = await capital_state(db_session)
    kwargs = dict(
        provider="argolink",
        reference="billing-error-20261006",
        amount_usdt=Decimal("15.73"),
        evidence="supplier-notice-2026-10-06",
        reason="Supplier statement; debit history pending",
    )
    first = await record_supplier_credit(db_session, **kwargs)
    await db_session.commit()
    second = await record_supplier_credit(db_session, **kwargs)
    assert first.id == second.id
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredit)) == 1
    assert first.status == "supplier_reported"
    after = await capital_state(db_session)
    assert after["safe"] == before["safe"]
    assert after["components"]["supplier_reported_provider_credits_usdt"] == Decimal("15.73")
    with pytest.raises(HTTPException) as conflict:
        await record_supplier_credit(db_session, **{**kwargs, "amount_usdt": Decimal("5")})
    assert conflict.value.status_code == 409


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 15.73])
async def test_supplier_credit_rejects_invalid_money(db_session, amount):
    from app.billing.provider_credits import record_supplier_credit

    with pytest.raises(ValueError):
        await record_supplier_credit(
            db_session, provider="argolink", reference="x", amount_usdt=amount, evidence="supplier", reason="notice"
        )
