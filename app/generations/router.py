from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, PartnerAuth, get_current_partner, get_partner_auth, require_admin
from app.billing.service import (
    apply_cost_coverage_change,
    apply_partner_balance_change,
    lock_partner_for_update,
    require_sufficient_balance,
    require_sufficient_cost_coverage,
)
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.generations.schemas import (
    GenerationCreate,
    GenerationRead,
    ProviderDispatchRead,
    ProviderPollRead,
)
from app.generations.service import (
    PRIMARY_PROVIDER,
    dispatch_generation_to_provider,
    has_active_provider_credential,
    has_provider_capability,
    poll_generation_provider,
)
from app.infrastructure.config import get_settings

router = APIRouter()
_RUB_QUANTUM = Decimal("0.01")


@router.post("", response_model=GenerationRead, status_code=status.HTTP_202_ACCEPTED)
async def create_generation(
    payload: GenerationCreate,
    db: DbSession,
    auth: PartnerAuth = Depends(get_partner_auth),
) -> Generation:
    partner = auth.partner
    existing = await _find_generation_by_idempotency_key(db, partner.id, payload.idempotency_key)
    if existing is not None:
        return existing

    model_result = await db.execute(
        select(Model, PartnerPrice)
        .join(PartnerPrice, PartnerPrice.model_id == Model.id)
        .where(
            Model.slug == payload.model_slug,
            Model.status == "production",
            PartnerPrice.mode == payload.mode,
            PartnerPrice.resolution == payload.resolution,
        )
    )
    row = model_result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_or_price_not_available")
    model, price = row
    billable_units = payload.duration_seconds if price.billing_unit == "second" else 1
    price_rub = Decimal(price.price_rub) * Decimal(billable_units)

    settings = get_settings()
    provider_cost_usdt = Decimal(price.provider_cost_usdt) * Decimal(billable_units)
    rub_per_usdt = Decimal(settings.rub_per_usdt)
    provider_cost_reserve_rub = (provider_cost_usdt * rub_per_usdt).quantize(
        _RUB_QUANTUM,
        rounding=ROUND_HALF_UP,
    )

    if not await has_active_provider_credential(db, partner.id):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="provider_temporarily_unavailable")
    if not await has_provider_capability(db, model.id, payload.mode, payload.resolution):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="capability_mismatch")

    locked_partner = await lock_partner_for_update(db, partner.id)
    existing = await _find_generation_by_idempotency_key(db, locked_partner.id, payload.idempotency_key)
    if existing is not None:
        return existing

    # Both funding gates happen before a generation UUID/row or either reserve is created.
    await require_sufficient_balance(locked_partner, price_rub)
    await require_sufficient_cost_coverage(locked_partner, provider_cost_reserve_rub)

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
        rub_per_usdt_snapshot=rub_per_usdt,
        provider_cost_reserve_rub=provider_cost_reserve_rub,
        prompt=payload.prompt,
        webhook_url_snapshot=auth.api_key.webhook_url,
        webhook_secret_encrypted_snapshot=auth.api_key.webhook_secret_encrypted,
        request_payload={
            "duration_seconds": payload.duration_seconds,
            "aspect_ratio": payload.aspect_ratio,
            "reference_images": [reference.model_dump() for reference in payload.reference_images],
            "billing_unit": price.billing_unit,
            "unit_price_rub": str(price.price_rub),
            "billable_units": billable_units,
        },
    )
    db.add(generation)
    await db.flush()
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
        allow_negative=False,
    )
    await db.refresh(generation)
    return generation


@router.post(
    "/{generation_id}/dispatch",
    response_model=ProviderDispatchRead,
    dependencies=[Depends(require_admin)],
)
async def dispatch_generation(generation_id: str, db: DbSession) -> ProviderDispatchRead:
    generation = await db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    if generation.status not in {"queued", "sent_to_provider"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="generation_not_dispatchable")
    attempt = await dispatch_generation_to_provider(db, generation, PRIMARY_PROVIDER)
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
    generation = await db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="generation_not_found")
    generation = await poll_generation_provider(db, generation, PRIMARY_PROVIDER)
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
