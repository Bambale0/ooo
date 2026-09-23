"""Explicit reconciliation of uncertain paid requests; no automatic submit replay."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import select

from app.api.dependencies import DbSession, require_admin
from app.billing.service import release_generation_reserves
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


@router.get("/reconciliation")
async def pending(db: DbSession):
    rows = (
        await db.execute(
            select(Generation)
            .where(
                Generation.status.in_(["submitting", "reconciliation_required"]),
            )
            .order_by(Generation.created_at)
            .limit(100)
        )
    ).scalars()
    return [
        {
            "id": g.id,
            "partner_id": g.partner_id,
            "model": g.model_slug,
            "status": g.status,
            "reserved_rub": str(g.partner_price_rub),
            "created_at": g.created_at,
        }
        for g in rows
    ]


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
        await db.execute(select(ProviderAttempt).where(ProviderAttempt.generation_id == g.id))
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
