from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner, PartnerCreditLimitHistory
from app.billing.models import CoverageLedgerEntry, LedgerEntry

if TYPE_CHECKING:
    from app.generations.models import Generation


async def lock_partner_for_update(db: AsyncSession, partner_id: str) -> Partner:
    result = await db.execute(
        select(Partner).where(Partner.id == partner_id).with_for_update().execution_options(populate_existing=True)
    )
    partner = result.scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return partner


def exact_rub_amount(value) -> Decimal:
    """RUB ledgers accept finite, representable, explicit kopeck amounts only."""
    if isinstance(value, (bool, float)):
        raise HTTPException(422, "invalid_ledger_amount")
    try:
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or abs(amount) >= Decimal("10000000000000000")
            or amount.quantize(Decimal("0.01")) != amount
        ):
            raise ValueError("invalid_ledger_amount")
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(422, "invalid_ledger_amount") from None
    return amount


def _ensure_same_financial_operation(entry, partner_id, amount, operation_type, generation_id):
    _ensure_entry_belongs_to_partner(entry.partner_id, partner_id)
    if entry.amount_rub != amount or entry.operation_type != operation_type or entry.generation_id != generation_id:
        raise HTTPException(409, "financial_idempotency_conflict")


async def _lock_generation_for_money(db, partner_id, generation_id, *, customer_debit=False):
    if generation_id is None:
        return
    from app.generations.models import Generation

    generation = await db.scalar(
        select(Generation)
        .where(Generation.id == generation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Preserve imported legacy references that predate durable generations.
    if generation is None:
        return
    if generation.partner_id != partner_id:
        raise HTTPException(409, "generation_partner_mismatch")
    if customer_debit and generation.billing_status in {"not_chargeable", "waived"}:
        raise HTTPException(409, "generation_not_chargeable")
    if customer_debit and generation.actual_charge_rub is None:
        from app.billing.models import CompensationAllocation

        compensated = await db.scalar(
            select(CompensationAllocation.id).where(CompensationAllocation.generation_id == generation.id).limit(1)
        )
        if compensated is not None:
            raise HTTPException(409, "compensated_generation_not_chargeable")


async def _lock_financial_key(db, namespace, key):
    # Partner -> key is consistent even for callers that already lock a partner.
    # Cross-partner key collisions then return a stable conflict, not a late
    # unique-constraint exception after an in-memory balance mutation.
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": namespace + ":" + key}
        )


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
    amount = exact_rub_amount(amount_rub)
    existing = await _find_ledger_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_same_financial_operation(existing, partner.id, amount, operation_type, generation_id)
        return existing

    await _lock_generation_for_money(db, partner.id, generation_id, customer_debit=amount < 0)
    locked_partner = await lock_partner_for_update(db, partner.id)
    await _lock_financial_key(db, "partner-ledger", idempotency_key)
    existing = await _find_ledger_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_same_financial_operation(existing, partner.id, amount, operation_type, generation_id)
        return existing

    next_balance = exact_rub_amount(Decimal(locked_partner.balance_rub) + amount)
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


async def apply_cost_coverage_change(
    db: AsyncSession,
    partner: Partner,
    amount_rub: Decimal,
    operation_type: str,
    idempotency_key: str,
    generation_id: str | None = None,
    description: str | None = None,
    *,
    allow_negative: bool = True,
) -> CoverageLedgerEntry:
    amount = exact_rub_amount(amount_rub)
    existing = await _find_coverage_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_same_financial_operation(existing, partner.id, amount, operation_type, generation_id)
        return existing

    await _lock_generation_for_money(db, partner.id, generation_id, customer_debit=False)
    locked_partner = await lock_partner_for_update(db, partner.id)
    await _lock_financial_key(db, "coverage-ledger", idempotency_key)
    existing = await _find_coverage_entry_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        _ensure_same_financial_operation(existing, partner.id, amount, operation_type, generation_id)
        return existing

    next_coverage = exact_rub_amount(Decimal(locked_partner.cost_coverage_rub) + amount)
    if not allow_negative and next_coverage < Decimal("0.00"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_temporarily_unavailable",
        )

    locked_partner.cost_coverage_rub = next_coverage
    partner.cost_coverage_rub = next_coverage
    entry = CoverageLedgerEntry(
        partner_id=locked_partner.id,
        operation_type=operation_type,
        amount_rub=amount,
        coverage_after_rub=next_coverage,
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

    from app.billing.compensations import compensation_totals

    await _lock_generation_for_money(db, generation.partner_id, generation.id)
    existing = await _find_generation_ledger_entry(db, generation.id, "generation_reserve_release")
    if existing is not None:
        return existing
    # An explicitly linked manual compensation has already returned cash. The
    # original reserve remains immutable; cleanup releases only the unpaid rest.
    totals = await compensation_totals(db, generation)
    amount = min(-Decimal(reserve.amount_rub), Decimal(totals["remaining_rub"]))
    if amount <= 0:
        return None
    partner = await _get_partner_or_404(db, generation.partner_id)
    return await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=amount,
        operation_type="generation_reserve_release",
        idempotency_key=f"generation-reserve-release:{generation.id}",
        generation_id=generation.id,
        description=reason,
        allow_negative=True,
    )


async def release_generation_cost_reserve(
    db: AsyncSession,
    generation: Generation,
    *,
    reason: str,
) -> CoverageLedgerEntry | None:
    reserve = await _find_generation_coverage_entry(db, generation.id, "provider_cost_reserve")
    if reserve is None or Decimal(reserve.amount_rub) >= Decimal("0.00"):
        return None

    partner = await _get_partner_or_404(db, generation.partner_id)
    return await apply_cost_coverage_change(
        db=db,
        partner=partner,
        amount_rub=-Decimal(reserve.amount_rub),
        operation_type="provider_cost_reserve_release",
        idempotency_key=f"provider-cost-reserve-release:{generation.id}",
        generation_id=generation.id,
        description=reason,
        allow_negative=True,
    )


async def release_generation_reserves(
    db: AsyncSession,
    generation: Generation,
    *,
    reason: str,
) -> None:
    await release_generation_reserve(db, generation, reason=reason)
    await release_generation_cost_reserve(db, generation, reason=reason)


async def settle_generation_reserve(
    db: AsyncSession,
    generation: Generation,
) -> LedgerEntry | None:
    if generation.billing_status in {"not_chargeable", "waived"}:
        raise HTTPException(409, "generation_not_chargeable")
    reserve = await _find_generation_ledger_entry(db, generation.id, "generation_reserve")
    if reserve is None:
        return None
    release = await _find_generation_ledger_entry(db, generation.id, "generation_reserve_release")
    if release is None:
        return None

    partner = await _get_partner_or_404(db, generation.partner_id)
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


async def settle_confirmed_partner_charge(db: AsyncSession, generation: Generation, charge: Decimal) -> None:
    """Bill confirmed usage once, with append-only compatibility for old holds.

    New generations have no partner reserve; old generations may have one
    which was already refunded. Original debits and reversals stay auditable.
    The caller holds the generation lock and sets actual_charge_rub after
    finishing the whole transaction.
    """
    if generation.billing_status in {"not_chargeable", "waived"}:
        raise HTTPException(409, "generation_not_chargeable")
    amount = Decimal(charge)
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_confirmed_partner_charge")
    reserve = await _find_generation_ledger_entry(db, generation.id, "generation_reserve")
    if reserve is not None and Decimal(reserve.amount_rub) >= 0:
        raise ValueError("invalid_original_partner_reserve")
    if reserve is not None:
        await settle_generation_reserve(db, generation)
    partner = await lock_partner_for_update(db, generation.partner_id)
    adjustment = (Decimal(generation.partner_price_rub) if reserve is not None else Decimal(0)) - amount
    await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=adjustment,
        operation_type="generation_usage_adjustment" if reserve is not None else "generation_usage_charge",
        idempotency_key=f"generation-usage:{generation.id}",
        generation_id=generation.id,
        description="Confirmed actual partner usage",
        allow_negative=True,
    )


async def settle_generation_cost_reserve(
    db: AsyncSession,
    generation: Generation,
) -> CoverageLedgerEntry | None:
    reserve = await _find_generation_coverage_entry(db, generation.id, "provider_cost_reserve")
    if reserve is None:
        return None
    release = await _find_generation_coverage_entry(db, generation.id, "provider_cost_reserve_release")
    if release is None:
        return None

    partner = await _get_partner_or_404(db, generation.partner_id)
    return await apply_cost_coverage_change(
        db=db,
        partner=partner,
        amount_rub=Decimal(reserve.amount_rub),
        operation_type="provider_cost_late_charge",
        idempotency_key=f"provider-cost-late-charge:{generation.id}",
        generation_id=generation.id,
        description="Late provider success after cost reserve release",
        allow_negative=True,
    )


async def settle_generation_reserves(db: AsyncSession, generation: Generation) -> None:
    await settle_generation_reserve(db, generation)
    await settle_generation_cost_reserve(db, generation)


async def require_sufficient_balance(partner: Partner, amount_rub: Decimal) -> None:
    if partner.available_balance_rub < amount_rub:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_balance")


async def set_partner_credit_limit(
    db: AsyncSession, partner_id: str, credit_limit_rub: Decimal, *, actor: str, reason: str
) -> Partner:
    limit = exact_rub_amount(credit_limit_rub)
    if limit < 0:
        raise HTTPException(422, "invalid_credit_limit")
    partner = await lock_partner_for_update(db, partner_id)
    if partner.status == "deleted":
        raise HTTPException(409, "partner_deleted")
    previous = Decimal(partner.credit_limit_rub)
    if previous != limit:
        db.add(PartnerCreditLimitHistory(
            partner_id=partner.id, old_limit_rub=previous, new_limit_rub=limit, actor=actor, reason=reason
        ))
        partner.credit_limit_rub = limit
        await db.flush()
    return partner


async def require_sufficient_cost_coverage(partner: Partner, amount_rub: Decimal) -> None:
    if Decimal(partner.cost_coverage_rub) < amount_rub:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_temporarily_unavailable",
        )


async def _get_partner_or_404(db: AsyncSession, partner_id: str) -> Partner:
    partner = await db.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return partner


async def _find_ledger_entry_by_idempotency_key(
    db: AsyncSession,
    idempotency_key: str,
) -> LedgerEntry | None:
    result = await db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key))
    return result.scalar_one_or_none()


async def _find_coverage_entry_by_idempotency_key(
    db: AsyncSession,
    idempotency_key: str,
) -> CoverageLedgerEntry | None:
    result = await db.execute(select(CoverageLedgerEntry).where(CoverageLedgerEntry.idempotency_key == idempotency_key))
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


async def _find_generation_coverage_entry(
    db: AsyncSession,
    generation_id: str,
    operation_type: str,
) -> CoverageLedgerEntry | None:
    result = await db.execute(
        select(CoverageLedgerEntry)
        .where(
            CoverageLedgerEntry.generation_id == generation_id,
            CoverageLedgerEntry.operation_type == operation_type,
        )
        .order_by(CoverageLedgerEntry.created_at.asc())
    )
    return result.scalars().first()


def _ensure_entry_belongs_to_partner(entry_partner_id: str, partner_id: str) -> None:
    if entry_partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ledger_idempotency_conflict")
