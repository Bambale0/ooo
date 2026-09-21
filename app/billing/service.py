from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.generations.models import Generation


async def lock_partner_for_update(db: AsyncSession, partner_id: str) -> Partner:
    result = await db.execute(
        select(Partner)
        .where(Partner.id == partner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    partner = result.scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return partner


async def apply_partner_balance_change(
    db: AsyncSession,
    partner: Partner,
    amount_rub: Decimal,
    operation_type: str,
    idempotency_key: str,
    generation_id: str | None = None,
    description: str | None = None,
    *,
    allow_negative: bool = True,
) -> LedgerEntry:
    existing = await _find_ledger_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_ledger_entry_belongs_to_partner(existing, partner.id)
        return existing

    locked_partner = await lock_partner_for_update(db, partner.id)

    existing = await _find_ledger_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_ledger_entry_belongs_to_partner(existing, partner.id)
        return existing

    amount = Decimal(amount_rub)
    next_balance = Decimal(locked_partner.balance_rub) + amount
    if not allow_negative and next_balance < Decimal("0.00"):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_balance")

    locked_partner.balance_rub = next_balance
    partner.balance_rub = next_balance
    entry = LedgerEntry(
        partner_id=locked_partner.id,
        operation_type=operation_type,
        amount_rub=amount,
        balance_after_rub=next_balance,
        idempotency_key=idempotency_key,
        generation_id=generation_id,
        description=description,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return entry


async def release_generation_reserve(
    db: AsyncSession,
    generation: Generation,
    *,
    reason: str,
) -> LedgerEntry | None:
    reserve = await _find_generation_ledger_entry(db, generation.id, "generation_reserve")
    if reserve is None or Decimal(reserve.amount_rub) >= Decimal("0.00"):
        return None

    partner = await db.get(Partner, generation.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")

    return await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=-Decimal(reserve.amount_rub),
        operation_type="generation_reserve_release",
        idempotency_key=f"generation-reserve-release:{generation.id}",
        generation_id=generation.id,
        description=reason,
        allow_negative=True,
    )


async def settle_generation_reserve(
    db: AsyncSession,
    generation: Generation,
) -> LedgerEntry | None:
    reserve = await _find_generation_ledger_entry(db, generation.id, "generation_reserve")
    if reserve is None:
        return None

    release = await _find_generation_ledger_entry(db, generation.id, "generation_reserve_release")
    if release is None:
        return None

    partner = await db.get(Partner, generation.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")

    return await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=Decimal(reserve.amount_rub),
        operation_type="generation_late_charge",
        idempotency_key=f"generation-late-charge:{generation.id}",
        generation_id=generation.id,
        description="Late provider success after reserve release",
        allow_negative=True,
    )


async def require_sufficient_balance(partner: Partner, amount_rub: Decimal) -> None:
    if Decimal(partner.balance_rub) < amount_rub:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_balance")


async def _find_ledger_entry_by_idempotency_key(
    db: AsyncSession,
    idempotency_key: str,
) -> LedgerEntry | None:
    result = await db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key))
    return result.scalar_one_or_none()


async def _find_generation_ledger_entry(
    db: AsyncSession,
    generation_id: str,
    operation_type: str,
) -> LedgerEntry | None:
    result = await db.execute(
        select(LedgerEntry)
        .where(
            LedgerEntry.generation_id == generation_id,
            LedgerEntry.operation_type == operation_type,
        )
        .order_by(LedgerEntry.created_at.asc())
    )
    return result.scalars().first()


def _ensure_ledger_entry_belongs_to_partner(entry: LedgerEntry, partner_id: str) -> None:
    if entry.partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ledger_idempotency_conflict")
