"""Explicit reconciliation of uncertain paid requests; no automatic submit replay."""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import func, or_, select

from app.api.dependencies import DbSession, require_admin
from app.billing.models import CoverageLedgerEntry
from app.billing.service import apply_cost_coverage_change, lock_partner_for_update, release_generation_reserves
from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.providers.models import ProviderAttempt
from app.providers.service import get_partner_provider_adapter
from app.webhooks.service import ensure_terminal_webhook_event

router = APIRouter(dependencies=[Depends(require_admin)])


class Reconciliation(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)
    outcome: str = Field(pattern="^(completed|not_accepted|attach_video)$")
    units: dict[str, StrictInt] | None = None
    provider_task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,255}$")


class AttemptCostReconciliation(BaseModel):
    outcome: str = Field(pattern="^(charged|free)$")
    provider_cost_usdt: Decimal | None = Field(default=None, ge=0)
    reason: str = Field(min_length=10, max_length=2000)


@router.get("/reconciliation")
async def pending(db: DbSession):
    unknown_generation_ids = select(ProviderAttempt.generation_id).where(ProviderAttempt.cost_status == "unknown")
    unconfirmed_ids = select(ProviderAttempt.generation_id).where(
        ProviderAttempt.status.in_(("submitting", "reconciliation_required")),
        (ProviderAttempt.provider_task_id.is_(None)) | (ProviderAttempt.provider_task_id == ""),
    )
    rows = (
        await db.execute(
            select(Generation)
            .where(
                or_(
                    Generation.status.in_(["submitting", "reconciliation_required"]),
                    (
                        Generation.client_reserve_released_at.is_not(None)
                        & (Generation.status == "sent_to_provider")
                        & Generation.id.in_(unconfirmed_ids)
                    ),
                    Generation.id.in_(unknown_generation_ids),
                ),
            )
            .order_by(Generation.created_at)
            .limit(100)
        )
    ).scalars()
    generations = list(rows)
    obligations: dict[str, list[ProviderAttempt]] = {}
    if generations:
        unknown_attempts = (
            await db.execute(
                select(ProviderAttempt)
                .where(
                    ProviderAttempt.generation_id.in_([g.id for g in generations]),
                    ProviderAttempt.cost_status == "unknown",
                )
                .order_by(ProviderAttempt.created_at, ProviderAttempt.id)
            )
        ).scalars()
        for attempt in unknown_attempts:
            obligations.setdefault(attempt.generation_id, []).append(attempt)
    return [
        {
            "id": g.id,
            "partner_id": g.partner_id,
            "model": g.model_slug,
            "status": g.status,
            "reserved_rub": "0.00" if g.client_reserve_released_at is not None else str(g.partner_price_rub),
            "original_reserved_rub": str(g.partner_price_rub),
            "financial_status": g.financial_status,
            "created_at": g.created_at,
            "provider_cost_obligations": [
                {
                    "attempt_id": attempt.id,
                    "provider": attempt.provider,
                    "cost_status": attempt.cost_status,
                    "reserved_usdt": str(attempt.cost_reserve_usdt or Decimal(0)),
                }
                for attempt in obligations.get(g.id, [])
            ],
        }
        for g in generations
    ]


@router.post("/reconciliation/{generation_id}/attempts/{attempt_id}/cost")
async def reconcile_attempt_cost(
    generation_id: str,
    attempt_id: str,
    payload: AttemptCostReconciliation,
    db: DbSession,
):
    generation = (
        await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
    ).scalar_one_or_none()
    if generation is None:
        raise HTTPException(404, "generation_not_found")
    attempt = (
        await db.execute(
            select(ProviderAttempt)
            .where(ProviderAttempt.id == attempt_id, ProviderAttempt.generation_id == generation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(404, "provider_attempt_not_found")

    if payload.outcome == "free":
        if payload.provider_cost_usdt not in (None, Decimal(0)):
            raise HTTPException(422, "free_attempt_cost_must_be_zero")
        actual_cost = Decimal(0)
        final_status = "confirmed_free"
    else:
        if payload.provider_cost_usdt is None:
            raise HTTPException(422, "provider_cost_usdt_required")
        actual_cost = Decimal(payload.provider_cost_usdt)
        final_status = "settled"

    if attempt.cost_status != "unknown":
        if attempt.cost_status == final_status and Decimal(attempt.provider_cost_usdt or 0) == actual_cost:
            return {"id": generation.id, "attempt_id": attempt.id, "cost_status": attempt.cost_status}
        raise HTTPException(409, "provider_attempt_cost_already_reconciled")

    held_usdt = Decimal(attempt.cost_reserve_usdt or 0)
    fx = Decimal(generation.rub_per_usdt_snapshot)
    held_rub = (held_usdt * fx).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_actual_cost = Decimal(generation.actual_provider_cost_usdt or 0) + actual_cost
    desired_coverage_net = -(total_actual_cost * fx).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    current_coverage_net = Decimal(
        await db.scalar(
            select(func.coalesce(func.sum(CoverageLedgerEntry.amount_rub), 0)).where(
                CoverageLedgerEntry.generation_id == generation.id
            )
        )
        or 0
    )
    coverage_delta = desired_coverage_net - current_coverage_net
    owner = await lock_partner_for_update(db, generation.partner_id)
    if coverage_delta:
        await apply_cost_coverage_change(
            db,
            owner,
            coverage_delta,
            "provider_attempt_cost_reconciliation",
            f"provider-attempt-cost:{attempt.id}",
            generation.id,
            payload.reason,
            allow_negative=True,
        )
    attempt.provider_cost_usdt = actual_cost
    attempt.cost_status = final_status
    generation.provider_cost_hold_usdt = max(
        Decimal(0),
        Decimal(generation.provider_cost_hold_usdt or 0) - held_usdt,
    )
    generation.provider_cost_hold_rub = max(
        Decimal("0.00"),
        Decimal(generation.provider_cost_hold_rub or 0) - held_rub,
    )
    generation.actual_provider_cost_usdt = total_actual_cost
    snapshot = generation.request_payload or {}
    history = snapshot.get("provider_cost_reconciliation_history", [])
    generation.request_payload = {
        **snapshot,
        "provider_cost_reconciliation_history": [
            *history,
            {
                "attempt_id": attempt.id,
                "provider": attempt.provider,
                "outcome": payload.outcome,
                "provider_cost_usdt": str(actual_cost),
                "reason": payload.reason,
                "actor": "admin_api",
            },
        ],
    }
    await db.flush()
    return {"id": generation.id, "attempt_id": attempt.id, "cost_status": attempt.cost_status}


@router.post("/reconciliation/{generation_id}")
async def reconcile(generation_id: str, payload: Reconciliation, db: DbSession):
    g = (
        await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "generation_not_found")
    snapshot = g.request_payload or {}
    history = snapshot.get(
        "reconciliation_history", [snapshot["reconciliation"]] if "reconciliation" in snapshot else []
    )
    if payload.model_dump() in history:
        return {"id": g.id, "status": g.status}
    if history and (history[-1]["outcome"] != "attach_video" or payload.outcome == "attach_video"):
        raise HTTPException(409, "already_reconciled")
    if g.status not in {"submitting", "reconciliation_required", "sent_to_provider"}:
        raise HTTPException(409, "request_not_reconcilable")
    attempt = (
        await db.execute(
            select(ProviderAttempt)
            .where(ProviderAttempt.generation_id == g.id)
            .order_by(ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(409, "submission_not_found")
    if payload.outcome == "attach_video":
        if not payload.provider_task_id or snapshot.get("protocol") in {
            "responses",
            "chat/completions",
            "messages",
            "images/edits",
            "images/generations",
        }:
            raise HTTPException(422, "video_task_required")
        attempt.provider_task_id = payload.provider_task_id
        from app.billing.client_release import clear_client_release_deadline

        clear_client_release_deadline(g)
        attempt.status = g.status = "processing"
        attempt.next_poll_at = attempt.next_attempt_at = None
    elif payload.outcome == "not_accepted":
        await release_generation_reserves(db, g, reason=payload.reason)
        g.status = attempt.status = "failed"
        g.public_error_code = "provider_rejected_request"
    else:
        rates = snapshot.get("rates", {})
        if (
            not payload.units
            or not set(payload.units) <= rates.keys()
            or any(isinstance(v, bool) or v < 0 for v in payload.units.values())
        ):
            raise HTTPException(422, "valid_usage_required")
        await settle_actual(db, g, payload.units)
        g.status = attempt.status = "completed"
    g.request_payload = {
        **snapshot,
        "reconciliation": payload.model_dump(),
        "reconciliation_history": [*history, payload.model_dump()],
        "reconciliation_actor": "admin_api",
    }
    await ensure_terminal_webhook_event(db, g)
    await db.flush()
    return {"id": g.id, "status": g.status}


@router.get("/partners/{partner_id}/upstream-usage")
async def upstream_usage(partner_id: str, db: DbSession):
    adapter = await get_partner_provider_adapter(db, partner_id, "argolink")
    data = await adapter.key_usage()
    # Admin-only; quota is the credential's procurement limit, not partner credit.
    return data
