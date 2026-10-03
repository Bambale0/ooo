import logging
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, PartnerAuth, get_current_partner, get_partner_auth, require_admin
from app.billing.service import (
    apply_cost_coverage_change,
    apply_partner_balance_change,
    lock_partner_for_update,
    require_sufficient_balance,
)
from app.catalog.access import RESTRICTED_STATUS, has_model_grant
from app.catalog.models import Model
from app.catalog.pricing import effective_partner_price
from app.generations.models import Generation
from app.generations.schemas import (
    GenerationCreate,
    GenerationRead,
    ProviderDispatchRead,
    ProviderPollRead,
)
from app.generations.service import (
    PRIMARY_PROVIDER,
    active_provider_for_generation,
    dispatch_generation_with_routing,
    fallback_cost_ceiling_for_request,
    has_active_provider_credential,
    has_provider_capability,
    poll_generation_provider,
)
from app.providers.base import ProviderGenerationRequest
from app.providers.video_contract import validate_video_request

logger = logging.getLogger(__name__)
router = APIRouter()
_RUB_QUANTUM = Decimal("0.01")


@router.post("", response_model=GenerationRead, status_code=status.HTTP_202_ACCEPTED)
async def create_generation(
    payload: GenerationCreate,
    db: DbSession,
    auth: PartnerAuth = Depends(get_partner_auth),
) -> Generation:
    partner = await lock_partner_for_update(db, auth.partner.id)
    existing = await _find_generation_by_idempotency_key(db, partner.id, payload.idempotency_key)
    if existing is not None:
        return existing

    model = (
        await db.execute(
            select(Model).where(
                Model.slug == payload.model_slug,
                Model.status.in_(["production", RESTRICTED_STATUS]),
            )
        )
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_or_price_not_available")
    price = await effective_partner_price(
        db,
        partner_id=partner.id,
        model_id=model.id,
        mode=payload.mode,
        resolution=payload.resolution,
    )
    if price is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_or_price_not_available")
    if model.status == RESTRICTED_STATUS and not await has_model_grant(db, model.id, partner.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_or_price_not_available")
    provider_request = ProviderGenerationRequest(
        generation_id="validation",
        model_slug=payload.model_slug,
        mode=payload.mode,
        resolution=payload.resolution,
        prompt=payload.prompt,
        duration_seconds=payload.duration_seconds,
        aspect_ratio=payload.aspect_ratio,
        reference_images=tuple(item.url for item in payload.reference_images),
        start_image=payload.start_image.url if payload.start_image else None,
        end_image=payload.end_image.url if payload.end_image else None,
    )
    try:
        validate_video_request(provider_request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    billable_units = payload.duration_seconds if price.billing_unit == "second" else 1
    price_rub = Decimal(price.price_rub) * Decimal(billable_units)

    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = await current_fx(db)
    provider_cost_usdt = Decimal(price.provider_cost_usdt) * Decimal(billable_units)
    rub_per_usdt = fx_data["rate"]
    provider_cost_reserve_usdt = provider_cost_usdt
    fallback = await fallback_cost_ceiling_for_request(
        db,
        partner_id=partner.id,
        model_id=model.id,
        request=provider_request,
    )
    if fallback is not None:
        fallback_cost = fallback[1]
        if fallback_cost * rub_per_usdt <= price_rub:
            provider_cost_reserve_usdt += fallback_cost
    provider_cost_reserve_rub = (provider_cost_reserve_usdt * rub_per_usdt).quantize(
        _RUB_QUANTUM,
        rounding=ROUND_HALF_UP,
    )

    if price_rub < provider_cost_usdt * rub_per_usdt:
        raise HTTPException(status_code=503, detail="provider_temporarily_unavailable")

    if not await has_active_provider_credential(db, partner.id):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="provider_temporarily_unavailable")
    if not await has_provider_capability(db, model.id, payload.mode, payload.resolution):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="capability_mismatch")

    locked_partner = await lock_partner_for_update(db, partner.id)
    if locked_partner.status != "active":
        raise HTTPException(403, "partner_not_active")
    existing = await _find_generation_by_idempotency_key(db, locked_partner.id, payload.idempotency_key)
    if existing is not None:
        return existing

    # Both funding gates happen before a generation UUID/row or either reserve is created.
    await require_sufficient_balance(locked_partner, price_rub)
    from app.billing.capital import require_provider_capital

    await require_provider_capital(db, provider_cost_reserve_usdt, partner_id=locked_partner.id)

    generation = Generation(
        partner_id=locked_partner.id,
        model_id=model.id,
        model_slug=model.slug,
        mode=payload.mode,
        resolution=payload.resolution,
        duration_seconds=payload.duration_seconds,
        aspect_ratio=payload.aspect_ratio,
        idempotency_key=payload.idempotency_key,
        partner_price_rub=price_rub,
        provider_cost_usdt_snapshot=provider_cost_usdt,
        provider_cost_reserve_usdt=provider_cost_reserve_usdt,
        rub_per_usdt_snapshot=rub_per_usdt,
        provider_cost_reserve_rub=provider_cost_reserve_rub,
        prompt=payload.prompt,
        webhook_url_snapshot=auth.api_key.webhook_url,
        webhook_secret_encrypted_snapshot=auth.api_key.webhook_secret_encrypted,
        request_payload={
            "fx": fx_snapshot(fx_data),
            "api_key_id": auth.api_key.id,
            "duration_seconds": payload.duration_seconds,
            "aspect_ratio": payload.aspect_ratio,
            "reference_images": [reference.model_dump() for reference in payload.reference_images],
            "start_image": payload.start_image.model_dump() if payload.start_image else None,
            "end_image": payload.end_image.model_dump() if payload.end_image else None,
            "billing_unit": price.billing_unit,
            "unit_price_rub": str(price.price_rub),
            "billable_units": billable_units,
        },
    )
    db.add(generation)
    await db.flush()
    from app.providers.circuit import require_admission

    await require_admission(db, generation.id, claim=False)
    await apply_partner_balance_change(
        db=db,
        partner=locked_partner,
        amount_rub=-price_rub,
        operation_type="generation_reserve",
        idempotency_key=f"generation-reserve:{generation.id}",
        generation_id=generation.id,
        description=f"Reserved partner price for {model.slug} ({billable_units} {price.billing_unit})",
        allow_negative=False,
    )
    await apply_cost_coverage_change(
        db=db,
        partner=locked_partner,
        amount_rub=-provider_cost_reserve_rub,
        operation_type="provider_cost_reserve",
        idempotency_key=f"provider-cost-reserve:{generation.id}",
        generation_id=generation.id,
        description="Reserved configured upstream procurement cost snapshot",
        allow_negative=True,
    )
    await db.refresh(generation)
    logger.info(
        "generation_reserved",
        extra={
            "trace_id": generation.id,
            "generation_id": generation.id,
            "partner_id": generation.partner_id,
            "api_key_id": auth.api_key.id,
            "model_id": generation.model_id,
        },
    )
    return generation


@router.post(
    "/{generation_id}/dispatch",
    response_model=ProviderDispatchRead,
    dependencies=[Depends(require_admin)],
)
async def dispatch_generation(generation_id: str, db: DbSession) -> ProviderDispatchRead:
    generation = (
        await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
    ).scalar_one_or_none()
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    if generation.status not in {"queued", "sent_to_provider"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="generation_not_dispatchable")
    attempt = await dispatch_generation_with_routing(db, generation)
    if attempt is None:
        raise HTTPException(503, "provider_temporarily_unavailable", headers={"Retry-After": "60"})
    await db.refresh(generation)
    return ProviderDispatchRead(
        generation_id=generation.id,
        status=generation.status,
        provider_attempt_status=attempt.status,
        public_error_code=attempt.public_error_code,
    )


@router.post(
    "/{generation_id}/poll-provider",
    response_model=ProviderPollRead,
    dependencies=[Depends(require_admin)],
)
async def poll_generation(generation_id: str, db: DbSession) -> ProviderPollRead:
    generation = (
        await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
    ).scalar_one_or_none()
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    provider = await active_provider_for_generation(db, generation.id)
    if provider is None:
        provider = (generation.request_payload or {}).get("fallback_provider") or PRIMARY_PROVIDER
    generation = await poll_generation_provider(db, generation, provider)
    return ProviderPollRead(
        generation_id=generation.id,
        status=generation.status,
        result_url=generation.result_url,
        public_error_code=generation.public_error_code,
    )


@router.post("/{generation_id}/webhook/resend")
async def resend_generation_webhook(
    generation_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> dict[str, object]:
    generation = await db.get(Generation, generation_id)
    if generation is None or generation.partner_id != partner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    from app.webhooks.service import request_manual_resend

    try:
        event = await request_manual_resend(db, generation_id=generation.id, partner_id=partner.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook_event_not_found") from exc
    return {
        "generation_id": generation.id,
        "event_id": event.id,
        "status": event.status,
        "attempt": event.attempt_count + 1,
    }


@router.get("/{generation_id}/trace")
async def read_generation_trace(
    generation_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> dict[str, object]:
    generation = await db.get(Generation, generation_id)
    if generation is None or generation.partner_id != partner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    from app.generations.trace import generation_trace

    trace = await generation_trace(db, generation, admin=False)
    logger.info(
        "generation_trace_read",
        extra={"trace_id": generation.id, "generation_id": generation.id, "partner_id": generation.partner_id},
    )
    return trace


@router.get("/admin/{generation_id}/trace", dependencies=[Depends(require_admin)])
async def read_generation_admin_trace(generation_id: str, db: DbSession) -> dict[str, object]:
    generation = await db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    from app.generations.trace import generation_trace

    trace = await generation_trace(db, generation, admin=True)
    logger.info(
        "generation_admin_trace_read",
        extra={"trace_id": generation.id, "generation_id": generation.id, "partner_id": generation.partner_id},
    )
    return trace


@router.get("/{generation_id}", response_model=GenerationRead)
async def read_generation(
    generation_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> Generation:
    generation = await db.get(Generation, generation_id)
    if generation is None or generation.partner_id != partner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    return generation


async def _find_generation_by_idempotency_key(
    db: DbSession,
    partner_id: str,
    idempotency_key: str,
) -> Generation | None:
    result = await db.execute(
        select(Generation).where(
            Generation.partner_id == partner_id,
            Generation.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


@router.post("/{generation_id}/cancel", response_model=GenerationRead)
async def cancel_generation(generation_id: str, db: DbSession, partner: Partner = Depends(get_current_partner)):
    from app.generations.service import cancel_before_submit

    generation = (
        await db.execute(
            select(Generation)
            .where(Generation.id == generation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if generation is None or generation.partner_id != partner.id:
        raise HTTPException(404, "generation_not_found")
    await cancel_before_submit(db, generation)
    return generation
