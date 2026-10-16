from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change


@pytest.mark.parametrize(
    "service,model", [(apply_partner_balance_change, LedgerEntry), (apply_cost_coverage_change, CoverageLedgerEntry)]
)
@pytest.mark.parametrize("changed", ["amount", "operation", "generation"])
async def test_money_idempotency_rejects_changed_semantics(db_session, service, model, changed):
    partner = Partner(
        telegram_id="idem-shape",
        company_name="Test",
        project_name="Test",
        balance_rub=Decimal("100"),
        cost_coverage_rub=Decimal("100"),
    )
    db_session.add(partner)
    await db_session.flush()
    args = dict(
        db=db_session,
        partner=partner,
        amount_rub=Decimal("10"),
        operation_type="manual_adjustment",
        idempotency_key="strict-financial-idem",
    )
    first = await service(**args)
    assert (await service(**args)).id == first.id
    mutated = {**args}
    mutated.update(
        {"amount_rub": Decimal("11")}
        if changed == "amount"
        else {"operation_type": "payment_credit"}
        if changed == "operation"
        else {"generation_id": "different-task"}
    )
    with pytest.raises(HTTPException) as error:
        await service(**mutated)
    assert error.value.status_code == 409
    assert len(list(await db_session.scalars(select(model)))) == 1
    assert (partner.balance_rub if model is LedgerEntry else partner.cost_coverage_rub) == Decimal("110")


@pytest.mark.parametrize("amount", [True, 0.1, Decimal("NaN"), Decimal("Infinity"), Decimal("0.001")])
async def test_ledger_rejects_ambiguous_money_before_mutating_balance(db_session, amount):
    partner = Partner(telegram_id="invalid-money", company_name="Test", project_name="Test", balance_rub=Decimal("100"))
    db_session.add(partner)
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await apply_partner_balance_change(db_session, partner, amount, "manual_adjustment", "invalid-money-idem")
    assert error.value.status_code == 422
    assert partner.balance_rub == Decimal("100")
    assert not list(await db_session.scalars(select(LedgerEntry)))
