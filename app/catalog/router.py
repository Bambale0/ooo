from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.dependencies import DbSession, require_admin
from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory
from app.catalog.schemas import ModelCreate, ModelEnableGateUpdate, ModelRead, PartnerPriceUpsert, PricingRead
from app.infrastructure.config import get_settings

router = APIRouter()


@router.post(
    "/models",
    response_model=ModelRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_model(payload: ModelCreate, db: DbSession) -> Model:
    if payload.status == "production":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="use_enable_endpoint_for_production")
    model = Model(**payload.model_dump())
    db.add(model)
    await db.flush()
    await db.refresh(model)
    return model


@router.get("/models", response_model=list[ModelRead])
async def list_models(db: DbSession) -> list[Model]:
    result = await db.execute(select(Model).where(Model.status == "production").order_by(Model.slug))
    return list(result.scalars().all())


@router.post(
    "/models/{model_slug}/enable-gates",
    response_model=ModelRead,
    dependencies=[Depends(require_admin)],
)
async def update_model_enable_gates(
    model_slug: str,
    payload: ModelEnableGateUpdate,
    db: DbSession,
) -> Model:
    model_result = await db.execute(select(Model).where(Model.slug == model_slug))
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    model.has_provider_integration = payload.has_provider_integration
    model.has_public_docs = payload.has_public_docs
    model.has_successful_smoke = payload.has_successful_smoke
    await db.flush()
    await db.refresh(model)
    return model


@router.post(
    "/models/{model_slug}/enable",
    response_model=ModelRead,
    dependencies=[Depends(require_admin)],
)
async def enable_model(model_slug: str, db: DbSession) -> Model:
    model_result = await db.execute(select(Model).where(Model.slug == model_slug))
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    await ensure_model_can_be_enabled(db, model)
    model.status = "production"
    await db.flush()
    await db.refresh(model)
    return model


@router.put("/pricing", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def upsert_price(payload: PartnerPriceUpsert, db: DbSession) -> None:
    model_result = await db.execute(select(Model).where(Model.slug == payload.model_slug))
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    rub_per_usdt = get_settings().rub_per_usdt
    provider_cost_rub = Decimal(payload.provider_cost_usdt) * rub_per_usdt
    if Decimal(payload.price_rub) < provider_cost_rub:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="partner_price_below_provider_cost")
    price_result = await db.execute(
        select(PartnerPrice).where(
            PartnerPrice.model_id == model.id,
            PartnerPrice.mode == payload.mode,
            PartnerPrice.resolution == payload.resolution,
        )
    )
    price = price_result.scalar_one_or_none()
    if price is None:
        db.add(
            PartnerPrice(
                model_id=model.id,
                mode=payload.mode,
                resolution=payload.resolution,
                price_rub=payload.price_rub,
                provider_cost_usdt=payload.provider_cost_usdt,
                billing_unit=payload.billing_unit,
            )
        )
        db.add(
            PartnerPriceHistory(
                model_id=model.id,
                mode=payload.mode,
                resolution=payload.resolution,
                old_price_rub=None,
                new_price_rub=payload.price_rub,
                old_provider_cost_usdt=None,
                new_provider_cost_usdt=payload.provider_cost_usdt,
                billing_unit=payload.billing_unit,
                rub_per_usdt_snapshot=rub_per_usdt,
            )
        )
    else:
        old_price_rub = price.price_rub
        old_provider_cost_usdt = price.provider_cost_usdt
        price.price_rub = payload.price_rub
        price.provider_cost_usdt = payload.provider_cost_usdt
        price.billing_unit = payload.billing_unit
        db.add(
            PartnerPriceHistory(
                model_id=model.id,
                mode=payload.mode,
                resolution=payload.resolution,
                old_price_rub=old_price_rub,
                new_price_rub=payload.price_rub,
                old_provider_cost_usdt=old_provider_cost_usdt,
                new_provider_cost_usdt=payload.provider_cost_usdt,
                billing_unit=payload.billing_unit,
                rub_per_usdt_snapshot=rub_per_usdt,
            )
        )


@router.get("/pricing", response_model=list[PricingRead])
async def list_pricing(db: DbSession) -> list[PricingRead]:
    result = await db.execute(
        select(Model, PartnerPrice)
        .join(PartnerPrice, PartnerPrice.model_id == Model.id)
        .where(Model.status == "production")
        .order_by(Model.slug, PartnerPrice.mode, PartnerPrice.resolution)
    )
    return [
        PricingRead(
            model_slug=model.slug,
            model_name=model.name,
            modality=model.modality,
            mode=price.mode,
            resolution=price.resolution,
            price_rub=price.price_rub,
            billing_unit=price.billing_unit,
        )
        for model, price in result.all()
    ]


async def ensure_model_can_be_enabled(db: DbSession, model: Model | ModelCreate) -> None:
    if not model.has_provider_integration:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_integration_gate_missing")
    if not model.has_public_docs:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="docs_gate_missing")
    if not model.has_successful_smoke:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="smoke_gate_missing")
    if isinstance(model, Model):
        price_result = await db.execute(select(PartnerPrice).where(PartnerPrice.model_id == model.id))
        if price_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="price_gate_missing")
