"""Operator refunds and explicit attribution of pre-existing manual credits."""

import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from app.billing.models import CompensationAllocation, LedgerEntry
from app.billing.service import apply_partner_balance_change, exact_rub_amount, lock_partner_for_update
from app.generations.models import Generation


def allocation_read(row):
    return {
        "id": row.id,
        "generation_id": row.generation_id,
        "partner_id": row.partner_id,
        "source_ledger_entry_id": row.source_ledger_entry_id,
        "amount_rub": f"{row.amount_rub:.2f}",
        "mode": row.mode,
        "actor": row.actor,
        "reason": row.reason,
    }


async def compensation_totals(db, generation):
    entries = list(
        await db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.generation_id == generation.id, LedgerEntry.partner_id == generation.partner_id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
    )
    allocations = list(
        await db.scalars(
            select(CompensationAllocation)
            .where(
                CompensationAllocation.generation_id == generation.id,
                CompensationAllocation.partner_id == generation.partner_id,
            )
            .order_by(CompensationAllocation.created_at, CompensationAllocation.id)
        )
    )
    debit = -sum((e.amount_rub for e in entries if e.amount_rub < 0), Decimal(0))
    credit = sum((e.amount_rub for e in entries if e.amount_rub > 0), Decimal(0))
    linked = sum((a.amount_rub for a in allocations if a.mode == "link"), Decimal(0))
    remaining = max(Decimal(0), debit - credit - linked)
    return {
        "generation_id": generation.id,
        "partner_id": generation.partner_id,
        "original_debits_rub": f"{debit:.2f}",
        "generation_credits_rub": f"{credit:.2f}",
        "linked_manual_rub": f"{linked:.2f}",
        "remaining_rub": f"{remaining:.2f}",
        "allocations": [allocation_read(row) for row in allocations],
        "ledger": [
            {"id": e.id, "operation_type": e.operation_type, "amount_rub": f"{e.amount_rub:.2f}"} for e in entries
        ],
    }


async def compensate_generation(db, generation_id, payload, *, actor):
    generation = await db.scalar(
        select(Generation)
        .where(Generation.id == generation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation is None:
        raise HTTPException(404, "generation_not_found")
    partner = await lock_partner_for_update(db, generation.partner_id)
    amount = exact_rub_amount(payload.amount_rub) if payload.amount_rub is not None else None
    semantics = {
        "generation_id": generation.id,
        "amount_rub": f"{amount:.2f}" if amount is not None else None,
        "source_ledger_entry_id": payload.source_ledger_entry_id,
        "reason": payload.reason,
        "actor": actor,
    }
    fingerprint = hashlib.sha256(json.dumps(semantics, sort_keys=True).encode()).hexdigest()
    existing = await db.scalar(
        select(CompensationAllocation).where(
            CompensationAllocation.partner_id == partner.id,
            CompensationAllocation.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise HTTPException(409, "compensation_idempotency_conflict")
        return existing
    if generation.status not in {"completed", "failed", "cancelled", "timeout", "reconciliation_required"}:
        raise HTTPException(409, "generation_still_active")
    totals = await compensation_totals(db, generation)
    remaining = Decimal(totals["remaining_rub"])
    if remaining <= 0:
        raise HTTPException(409, "generation_already_compensated")
    amount = remaining if amount is None else amount
    if amount <= 0 or amount > remaining:
        raise HTTPException(409, "compensation_exceeds_remaining_charge")
    if payload.source_ledger_entry_id:
        source = await db.scalar(
            select(LedgerEntry).where(LedgerEntry.id == payload.source_ledger_entry_id).with_for_update()
        )
        if (
            source is None
            or source.partner_id != partner.id
            or source.operation_type != "manual_adjustment"
            or source.generation_id is not None
            or source.amount_rub <= 0
        ):
            raise HTTPException(409, "invalid_compensation_source")
        allocated = Decimal(
            await db.scalar(
                select(func.coalesce(func.sum(CompensationAllocation.amount_rub), 0)).where(
                    CompensationAllocation.source_ledger_entry_id == source.id
                )
            )
            or 0
        )
        if amount > source.amount_rub - allocated:
            raise HTTPException(409, "compensation_source_exhausted")
        mode = "link"
    else:
        mode = "credit"
        source = await apply_partner_balance_change(
            db,
            partner,
            amount,
            "generation_refund",
            "generation-compensation:"
            + hashlib.sha256((partner.id + ":" + payload.idempotency_key).encode()).hexdigest(),
            generation.id,
            payload.reason,
        )
    allocation = CompensationAllocation(
        partner_id=partner.id,
        generation_id=generation.id,
        source_ledger_entry_id=source.id,
        amount_rub=amount,
        mode=mode,
        idempotency_key=payload.idempotency_key,
        request_hash=fingerprint,
        actor=actor,
        reason=payload.reason,
    )
    db.add(allocation)
    await db.flush()
    # No old manual credit is guessed from descriptions; this link is explicit.
    # Unbilled legacy work is closed only when its original debit is fully covered.
    if generation.actual_charge_rub is None and amount == remaining:
        from app.infrastructure.retry import utc_now
        from app.providers.models import ProviderAttempt
        from app.webhooks.service import ensure_terminal_webhook_event

        generation.status = "failed"
        generation.billing_status = "not_chargeable"
        generation.client_finalized_at = generation.client_finalized_at or utc_now()
        generation.public_error_code = "request_compensated"
        attempts = list(await db.scalars(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation.id)))
        unresolved = [a for a in attempts if a.cost_status not in {"settled", "confirmed_free"}]
        if unresolved:
            obligation = generation.provider_cost_reserve_usdt or generation.provider_cost_usdt_snapshot
            for attempt in unresolved:
                attempt.status = "reconciliation_required"
                attempt.cost_status = "unknown"
                attempt.next_poll_at = attempt.next_attempt_at = None
            if not any(a.cost_reserve_usdt for a in unresolved):
                unresolved[-1].cost_reserve_usdt = obligation
            generation.provider_cost_hold_usdt = sum(
                (a.cost_reserve_usdt or Decimal(0) for a in unresolved), Decimal(0)
            )
            generation.provider_cost_hold_rub = (
                generation.provider_cost_hold_usdt * generation.rub_per_usdt_snapshot
            ).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
        await ensure_terminal_webhook_event(db, generation)
    await db.flush()
    return allocation
