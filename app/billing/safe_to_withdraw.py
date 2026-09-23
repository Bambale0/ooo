"""Read-only treasury calculation and append-only records; never transfers funds."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts.models import Partner, ProfitWithdrawal
from app.billing.capital import capital_state, lock_capital


async def calculate_safe_to_withdraw(db):
    state = await capital_state(db)
    return {
        "safe_to_withdraw_usdt": state["safe"],
        "components": state.get("components", {}),
        "freshness": state["freshness"],
        "wallet_age_seconds": state["wallet_age_seconds"],
    }


async def record_profit_withdrawal(
    db,
    *,
    amount_usdt: Decimal,
    reason: str,
    partner_id: str,
    idempotency_key: str,
    override_reason: str | None = None,
    correction_for_id: str | None = None,
):
    await lock_capital(db)
    existing = (
        await db.execute(select(ProfitWithdrawal).where(ProfitWithdrawal.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing:
        if (
            existing.amount_usdt,
            existing.reason,
            existing.partner_id,
            existing.correction_for_id,
            existing.override_reason,
        ) != (amount_usdt, reason, partner_id, correction_for_id, override_reason):
            raise HTTPException(409, "idempotency_conflict")
        return withdrawal_read(existing)
    if await db.get(Partner, partner_id) is None:
        raise HTTPException(404, "partner_not_found")
    if correction_for_id:
        original = (
            await db.execute(select(ProfitWithdrawal).where(ProfitWithdrawal.id == correction_for_id).with_for_update())
        ).scalar_one_or_none()
        if original is None or original.correction_for_id or original.partner_id != partner_id or amount_usdt >= 0:
            raise HTTPException(422, "invalid_withdrawal_correction")
        corrected = Decimal(
            (
                await db.execute(
                    select(func.coalesce(func.sum(ProfitWithdrawal.amount_usdt), 0)).where(
                        ProfitWithdrawal.correction_for_id == original.id
                    )
                )
            ).scalar()
        )
        if original.amount_usdt + corrected + amount_usdt < 0:
            raise HTTPException(409, "correction_exceeds_original")
    else:
        if amount_usdt <= 0:
            raise HTTPException(422, "withdrawal_must_be_positive")
        state = await capital_state(db)
        if (state["safe"] is None or amount_usdt > state["safe"]) and not override_reason:
            raise HTTPException(409, "withdrawal_override_reason_required")
    withdrawal = ProfitWithdrawal(
        partner_id=partner_id,
        amount_usdt=amount_usdt,
        reason=reason,
        idempotency_key=idempotency_key,
        override_reason=override_reason,
        correction_for_id=correction_for_id,
    )
    db.add(withdrawal)
    await db.flush()
    await db.refresh(withdrawal)
    return withdrawal_read(withdrawal)


def withdrawal_read(row):
    return {
        "id": row.id,
        "amount_usdt": row.amount_usdt,
        "reason": row.reason,
        "idempotency_key": row.idempotency_key,
        "correction_for_id": row.correction_for_id,
        "override_reason": row.override_reason,
        "created_at": row.created_at,
    }
