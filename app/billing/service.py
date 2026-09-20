from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.billing.models import LedgerEntry


async def apply_partner_balance_change(
    db: AsyncSession,
    partner: Partner,
    amount_rub: Decimal,
    operation_type: str,
    idempotency_key: str,
    generation_id: str | None = None,
    description: str | None = None,
) -> LedgerEntry:
    existing_result = await db.execute(
        select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing

    partner.balance_rub = Decimal(partner.balance_rub) + amount_rub
    entry = LedgerEntry(
        partner_id=partner.id,
        operation_type=operation_type,
        amount_rub=amount_rub,
        balance_after_rub=partner.balance_rub,
        idempotency_key=idempotency_key,
        generation_id=generation_id,
        description=description,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return entry


async def require_sufficient_balance(partner: Partner, amount_rub: Decimal) -> None:
    if Decimal(partner.balance_rub) < amount_rub:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_balance")
