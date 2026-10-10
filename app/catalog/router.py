from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, require_admin
from app.catalog.access import grant_model_access, revoke_model_access
from app.catalog.models import (
    Model,
    PartnerModelGrant,
    PartnerPrice,
    PartnerPriceHistory,
    PartnerPriceOverrideHistory,
    PartnerPriceSnapshot,
)
from app.catalog.pricing import publish_global_partner_price, snapshot_price_for_existing_partners
from app.catalog.procurement import supports_free_rate
from app.catalog.schemas import (
    ModelCreate,
    ModelEnableGateUpdate,
    ModelGrantCreate,
    ModelGrantRead,
    ModelRead,
    PartnerPriceOverrideRead,
    PartnerPriceOverrideUpsert,
    PartnerPriceUpsert,
    PricingRead,
)
from app.catalog.video_edit_pricing import EDIT_MODE, EDIT_MODEL, edit_markup, retail_rate
from app.contracts.registry import MODELS

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
    if (
        model.slug not in MODELS
        or model.modality != {"chat": "llm", "image": "image", "video": "video"}[MODELS[model.slug]["category"]]
    ):
        raise HTTPException(status_code=409, detail="model_contract_not_supported")
    await ensure_model_can_be_enabled(db, model)
    model.status = "production"
    await db.flush()
    await db.refresh(model)
    return model


@router.post(
    "/models/{model_slug}/enable-restricted",
    response_model=ModelRead,
    dependencies=[Depends(require_admin)],
)
async def enable_restricted_model(model_slug: str, db: DbSession) -> Model:
    """Move a model into `restricted` status: runnable only for granted partners.

    Public documentation is deliberately skipped, because a restricted model must
    not appear in /models, /pricing or the public reference. Provider integration
    and a confirmed smoke are still required; a 409 is returned otherwise.
    """
    model = (await db.execute(select(Model).where(Model.slug == model_slug))).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    if (
        model.slug not in MODELS
        or model.modality != {"chat": "llm", "image": "image", "video": "video"}[MODELS[model.slug]["category"]]
    ):
        raise HTTPException(status_code=409, detail="model_contract_not_supported")
    from app.billing.fx import current_fx

    fx = (await current_fx(db))["rate"]
    prices = list((await db.execute(select(PartnerPrice).where(PartnerPrice.model_id == model.id))).scalars())
    if not prices:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="price_gate_missing")
    if any(retail_rate(model.slug, p, fx) < p.provider_cost_usdt * fx for p in prices):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="economic_gate_missing")
    if not model.has_provider_integration:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_integration_gate_missing")
    if not model.has_successful_smoke:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="smoke_gate_missing")
    model.status = "restricted"
    await db.flush()
    await db.refresh(model)
    return model


@router.post(
    "/models/{model_slug}/grants",
    response_model=ModelGrantRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_model_grant(model_slug: str, payload: ModelGrantCreate, db: DbSession) -> dict:
    grant = await grant_model_access(
        db, model_slug=model_slug, partner_id=payload.partner_id, reason=payload.reason, actor="admin_api"
    )
    return {
        "id": grant.id,
        "model_slug": model_slug,
        "partner_id": grant.partner_id,
        "granted_by": grant.granted_by,
        "reason": grant.reason,
        "revoked_at": grant.revoked_at,
        "created_at": grant.created_at,
    }


@router.delete(
    "/models/{model_slug}/grants/{partner_id}",
    response_model=ModelGrantRead,
    dependencies=[Depends(require_admin)],
)
async def delete_model_grant(model_slug: str, partner_id: str, db: DbSession) -> dict:
    grant = await revoke_model_access(
        db, model_slug=model_slug, partner_id=partner_id, reason="revoked", actor="admin_api"
    )
    return {
        "id": grant.id,
        "model_slug": model_slug,
        "partner_id": grant.partner_id,
        "granted_by": grant.granted_by,
        "reason": grant.reason,
        "revoked_at": grant.revoked_at,
        "created_at": grant.created_at,
    }


@router.get(
    "/models/{model_slug}/grants",
    response_model=list[ModelGrantRead],
    dependencies=[Depends(require_admin)],
)
async def list_model_grants(model_slug: str, db: DbSession) -> list[dict]:
    model = (await db.execute(select(Model).where(Model.slug == model_slug))).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    grants = (
        await db.execute(
            select(PartnerModelGrant)
            .where(PartnerModelGrant.model_id == model.id, PartnerModelGrant.revoked_at.is_(None))
            .order_by(PartnerModelGrant.created_at)
        )
    ).scalars()
    return [
        {
            "id": g.id,
            "model_slug": model_slug,
            "partner_id": g.partner_id,
            "granted_by": g.granted_by,
            "reason": g.reason,
            "revoked_at": g.revoked_at,
            "created_at": g.created_at,
        }
        for g in grants
    ]


@router.put(
    "/pricing/partners/{partner_id}",
    response_model=PartnerPriceOverrideRead,
    dependencies=[Depends(require_admin)],
)
async def upsert_partner_price_override(
    partner_id: str,
    payload: PartnerPriceOverrideUpsert,
    db: DbSession,
) -> PartnerPriceOverrideRead:
    """Set a durable price for one partner without repricing prior generations."""
    partner = await db.get(Partner, partner_id)
    if partner is None or partner.status == "deleted":
        raise HTTPException(status_code=404, detail="partner_not_found")
    model = (await db.execute(select(Model).where(Model.slug == payload.model_slug))).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="model_not_found")
    template = (
        await db.execute(
            select(PartnerPrice)
            .where(
                PartnerPrice.model_id == model.id,
                PartnerPrice.mode == payload.mode,
                PartnerPrice.resolution == payload.resolution,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="price_variant_not_found")
    snapshot = await db.get(PartnerPriceSnapshot, (partner_id, template.id), with_for_update=True)
    if snapshot is None:
        raise HTTPException(status_code=409, detail="partner_price_snapshot_missing")

    from app.billing.fx import current_fx

    fx_rate = Decimal((await current_fx(db))["rate"])
    if payload.price_rub < template.provider_cost_usdt * fx_rate:
        raise HTTPException(status_code=409, detail="partner_price_below_provider_cost")

    if snapshot.price_rub != payload.price_rub or not snapshot.is_custom:
        db.add(
            PartnerPriceOverrideHistory(
                partner_id=partner_id,
                partner_price_id=template.id,
                old_price_rub=snapshot.price_rub,
                new_price_rub=payload.price_rub,
                actor="admin_api",
                reason=payload.reason,
            )
        )
        snapshot.price_rub = payload.price_rub
        snapshot.is_custom = True
        await db.flush()

    return PartnerPriceOverrideRead(
        partner_id=partner_id,
        model_slug=model.slug,
        mode=template.mode,
        resolution=template.resolution,
        price_rub=snapshot.price_rub,
        billing_unit=template.billing_unit,
        is_custom=True,
    )


@router.put("/pricing", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def upsert_price(payload: PartnerPriceUpsert, db: DbSession) -> None:
    model_result = await db.execute(select(Model).where(Model.slug == payload.model_slug))
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = await current_fx(db)
    rub_per_usdt = fx_data["rate"]
    provider_cost_rub = Decimal(payload.provider_cost_usdt) * rub_per_usdt
    if Decimal(payload.price_rub) < provider_cost_rub:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="partner_price_below_provider_cost")
    price_result = await db.execute(
        select(PartnerPrice).where(
            PartnerPrice.model_id == model.id,
            PartnerPrice.mode == payload.mode,
            PartnerPrice.resolution == payload.resolution,
        ).with_for_update()
    )
    price = price_result.scalar_one_or_none()
    if price is not None:
        # A custom rate must retain its billing unit (e.g. RUB per second).
        if payload.billing_unit != price.billing_unit:
            custom_partner_id = (
                await db.execute(
                    select(PartnerPriceSnapshot.partner_id)
                    .where(
                        PartnerPriceSnapshot.partner_price_id == price.id,
                        PartnerPriceSnapshot.is_custom.is_(True),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if custom_partner_id is not None:
                raise HTTPException(status_code=409, detail="custom_partner_billing_unit_conflict")
        # A procurement update must not silently make a negotiated rate loss-making.
        below_cost_partner_id = (
            await db.execute(
                select(PartnerPriceSnapshot.partner_id)
                .where(
                    PartnerPriceSnapshot.partner_price_id == price.id,
                    PartnerPriceSnapshot.is_custom.is_(True),
                    PartnerPriceSnapshot.price_rub < provider_cost_rub,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if below_cost_partner_id is not None:
            raise HTTPException(status_code=409, detail="custom_partner_price_below_provider_cost")
    if price is None:
        price = PartnerPrice(
            model_id=model.id,
            mode=payload.mode,
            resolution=payload.resolution,
            price_rub=payload.price_rub,
            provider_cost_usdt=payload.provider_cost_usdt,
            billing_unit=payload.billing_unit,
        )
        db.add(price)
        await db.flush()
        await snapshot_price_for_existing_partners(db, price)
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
                fx_snapshot=fx_snapshot(fx_data),
            )
        )
    else:
        old_price_rub = price.price_rub
        old_provider_cost_usdt = price.provider_cost_usdt
        await publish_global_partner_price(db, price, payload.price_rub)
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
                fx_snapshot=fx_snapshot(fx_data),
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
    rows = [
        (model, price)
        for model, price in result.all()
        if not (model.slug == EDIT_MODEL and price.mode == EDIT_MODE and edit_markup(model.slug) is None)
    ]
    fx = Decimal(1)
    if any(model.slug == EDIT_MODEL and price.mode == EDIT_MODE for model, price in rows):
        from app.billing.fx import current_fx

        fx = (await current_fx(db))["rate"]
    return [
        PricingRead(
            model_slug=model.slug,
            model_name=model.name,
            modality=model.modality,
            mode=price.mode,
            resolution=price.resolution,
            price_rub=retail_rate(model.slug, price, fx),
            billing_unit=price.billing_unit,
        )
        for model, price in rows
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
        prices = list(price_result.scalars())
        if not prices:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="price_gate_missing")
        from app.billing.fx import current_fx

        fx = (await current_fx(db))["rate"]
        if any(
            not p.price_rub.is_finite()
            or not p.provider_cost_usdt.is_finite()
            or p.price_rub < 0
            or p.provider_cost_usdt < 0
            or (
                (p.price_rub == 0 or p.provider_cost_usdt == 0)
                and not supports_free_rate(model.slug, p.mode, p.resolution, p.billing_unit)
            )
            or retail_rate(model.slug, p, fx) < p.provider_cost_usdt * fx
            for p in prices
        ):
            raise HTTPException(409, "economic_gate_missing")
        if model.modality in {"llm", "image"}:
            from app.catalog.sync import variants

            required = {(m, r, u) for m, r, u, _ in variants(MODELS[model.slug])}
            actual = {(p.mode, p.resolution, p.billing_unit) for p in prices}
            if not required <= actual:
                raise HTTPException(409, "complete_price_schedule_required")


@router.get("/contracts", dependencies=[Depends(require_admin)])
async def reviewed_contracts():
    from app.catalog.sync import variants
    from app.contracts.registry import CATALOG, MODELS, OBSERVATIONS

    return {
        "revision": CATALOG["revision"],
        "manual_procurement_review": OBSERVATIONS["manual_procurement_review"],
        "models": [
            {
                "model": slug,
                "category": entry["category"],
                "endpoint": entry["endpoint"],
                "required_prices": [
                    {"mode": m, "resolution": r, "billing_unit": u, "provider_cost_usdt": str(c)}
                    for m, r, u, c in variants(entry)
                ],
            }
            for slug, entry in MODELS.items()
        ],
    }


@router.get("/contracts/drift", dependencies=[Depends(require_admin)])
async def contract_drift():
    from app.catalog.sync import check_catalog_drift

    return await check_catalog_drift()


@router.post("/contracts/import", dependencies=[Depends(require_admin)])
async def import_contracts(db: DbSession):
    from app.catalog.sync import import_reviewed_catalog

    return await import_reviewed_catalog(db)
