"""Record supplier compensation separately from generation and cash ledgers."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.billing.capital import lock_capital
from app.generations.models import Generation
from app.providers.models import ProviderAttempt, ProviderCredit


async def record_supplier_credit(db, *, provider, reference, amount_usdt, evidence, reason):
    if (
        not isinstance(amount_usdt, Decimal)
        or not amount_usdt.is_finite()
        or not Decimal(0) < amount_usdt < Decimal("1e18")
        or not provider.strip()
        or len(provider) > 80
        or not reference.strip()
        or len(reference) > 160
        or not evidence.strip()
        or not reason.strip()
    ):
        raise ValueError("invalid_supplier_credit")
    await lock_capital(db)
    existing = await db.scalar(
        select(ProviderCredit).where(ProviderCredit.provider == provider, ProviderCredit.reference == reference)
    )
    if existing is not None:
        if (existing.amount_usdt, existing.evidence, existing.reason) != (amount_usdt, evidence, reason):
            raise HTTPException(409, "supplier_credit_idempotency_conflict")
        return existing
    row = ProviderCredit(
        provider=provider,
        reference=reference,
        amount_usdt=amount_usdt,
        evidence=evidence,
        reason=reason,
        status="supplier_reported",
    )
    db.add(row)
    await db.flush()
    return row


async def backfill_primary_cost_estimates(db):
    """Enrich single-attempt history, never recalculate generation money or rates."""
    from sqlalchemy.orm import aliased

    other = aliased(ProviderAttempt)
    rows = (
        await db.execute(
            select(ProviderAttempt, Generation)
            .join(Generation, Generation.id == ProviderAttempt.generation_id)
            .where(
                Generation.model_slug == "seedance-2.5",
                Generation.status == "completed",
                Generation.actual_provider_cost_usdt.is_not(None),
                ProviderAttempt.provider == "argolink",
                ProviderAttempt.status == "completed",
                ProviderAttempt.cost_status.is_(None),
                ~select(other.id).where(other.generation_id == Generation.id, other.id != ProviderAttempt.id).exists(),
            )
            .with_for_update(of=ProviderAttempt)
        )
    ).all()
    for attempt, generation in rows:
        attempt.provider_cost_usdt = generation.actual_provider_cost_usdt
        attempt.cost_status = "estimated"
        attempt.usage_snapshot = generation.usage_snapshot
    await db.flush()
    return len(rows)
