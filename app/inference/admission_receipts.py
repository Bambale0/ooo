"""Durable, unbilled rejection fences for opt-in native API consumers."""

import hashlib
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import lock_partner_for_update
from app.catalog.models import Model
from app.generations.models import Generation
from app.inference.service import _existing_generation, fingerprint
from app.infrastructure.retry import utc_now

REJECTION_KEY = "native_admission_rejection"
REJECTION_CODE = "provider_temporarily_unavailable"


async def fence_rejected_admission(
    db: AsyncSession,
    *,
    partner_id: str,
    api_key_id: str,
    protocol: str,
    body: dict[str, Any],
    idempotency_key: str,
    client_request_id: str,
    files_digest: str = "",
) -> Generation:
    """Recheck the logical key under its existing partner lock after rollback.

    A concurrent retry can win between rejection and re-locking. Return that
    accepted generation, never a no-acceptance receipt. Otherwise commit an
    unbilled terminal generation as the existing idempotency tombstone before
    allowing external failover. No provider attempt or ledger entry is created.
    """
    await lock_partner_for_update(db, partner_id)
    request_hash = fingerprint(protocol, body, files_digest)
    existing = await _existing_generation(db, partner_id, idempotency_key, request_hash, client_request_id)
    if existing is not None:
        await db.commit()
        return existing
    model = await db.scalar(select(Model).where(Model.slug == body["model"]))
    if model is None:
        # Cannot establish a durable identity; remain ambiguous to the caller.
        raise HTTPException(503, REJECTION_CODE)
    generation = Generation(
        partner_id=partner_id,
        model_id=model.id,
        model_slug=model.slug,
        mode=protocol,
        resolution=str(body.get("resolution") or "default"),
        status="failed",
        billing_status="not_chargeable",
        client_finalized_at=utc_now(),
        idempotency_key=idempotency_key,
        partner_price_rub=Decimal(0),
        provider_cost_usdt_snapshot=Decimal(0),
        provider_cost_reserve_usdt=Decimal(0),
        actual_charge_rub=Decimal(0),
        actual_provider_cost_usdt=Decimal(0),
        public_error_code=REJECTION_CODE,
        prompt="",
        request_payload={
            "protocol": protocol,
            "request_hash": request_hash,
            "client_request_id": client_request_id,
            "api_key_id": api_key_id,
            REJECTION_KEY: {"version": 1},
        },
    )
    db.add(generation)
    await db.commit()
    return generation


def rejected_admission_response(
    generation: Generation,
    *,
    trace_id: str,
    receipt_requested: bool,
) -> JSONResponse | None:
    snapshot = generation.request_payload or {}
    if snapshot.get(REJECTION_KEY) != {"version": 1}:
        return None
    if (
        generation.status != "failed"
        or generation.billing_status != "not_chargeable"
        or generation.actual_charge_rub != 0
        or generation.public_error_code != REJECTION_CODE
    ):
        raise RuntimeError("Invalid durable rejection fence")
    headers = {"X-Request-Id": trace_id}
    if receipt_requested:
        headers.update(
            {
                "X-Submission-State": "not-accepted-v1",
                "X-Client-Request-Id": snapshot["client_request_id"],
                "X-Idempotency-Key-Sha256": hashlib.sha256(generation.idempotency_key.encode()).hexdigest(),
            }
        )
    return JSONResponse({"detail": REJECTION_CODE}, status_code=503, headers=headers)
