"""Scoped Kling 4.0 registration with reviewed prices, but no smoke claim or enablement."""

import argparse
import asyncio
import json
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.billing.fx import current_fx
from app.catalog.models import Model, PartnerPrice
from app.catalog.router import upsert_price
from app.catalog.schemas import PartnerPriceUpsert
from app.catalog.sync import variants
from app.contracts.registry import MODELS
from app.providers.models import ProviderModelCapability

MODEL_SLUG = "kling-4.0"
RETAIL_MULTIPLIER = Decimal("1") + Decimal(str(MODELS[MODEL_SLUG]["retail_markup_percent"])) / Decimal("100")


async def price_plan(db) -> list[PartnerPriceUpsert]:
    """Return the four current-FX tariffs without writing any state."""
    fx = Decimal((await current_fx(db))["rate"])
    return [
        PartnerPriceUpsert(
            model_slug=MODEL_SLUG,
            mode=mode,
            resolution=resolution,
            billing_unit=unit,
            provider_cost_usdt=cost,
            price_rub=(cost * fx * RETAIL_MULTIPLIER).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        for mode, resolution, unit, cost in variants(MODELS[MODEL_SLUG])
    ]


async def register_kling_4(db) -> Model:
    """Register the reviewed contract and capabilities in one caller-owned transaction."""
    plan = await price_plan(db)
    model = await db.scalar(select(Model).where(Model.slug == MODEL_SLUG).with_for_update())
    if model is None:
        model = Model(
            slug=MODEL_SLUG,
            name="Kling 4.0",
            modality="video",
            status="draft",
            has_provider_integration=True,
            has_public_docs=True,
            has_successful_smoke=False,
        )
        db.add(model)
        await db.flush()
    elif model.modality != "video":
        raise ValueError("model_modality_conflict")
    else:
        model.has_provider_integration = True
        model.has_public_docs = True

    for payload in plan:
        price = await db.scalar(
            select(PartnerPrice).where(
                PartnerPrice.model_id == model.id,
                PartnerPrice.mode == payload.mode,
                PartnerPrice.resolution == payload.resolution,
            ).with_for_update()
        )
        if price is None or (
            price.price_rub, price.provider_cost_usdt, price.billing_unit
        ) != (payload.price_rub, payload.provider_cost_usdt, payload.billing_unit):
            await upsert_price(payload, db)
        capability = await db.scalar(
            select(ProviderModelCapability).where(
                ProviderModelCapability.provider == "argolink",
                ProviderModelCapability.model_id == model.id,
                ProviderModelCapability.mode == payload.mode,
                ProviderModelCapability.resolution == payload.resolution,
            )
        )
        if capability is None:
            db.add(
                ProviderModelCapability(
                    provider="argolink",
                    model_id=model.id,
                    mode=payload.mode,
                    resolution=payload.resolution,
                    is_active=True,
                )
            )
    await db.flush()
    return model


async def run(*, apply: bool = False) -> None:
    from app.infrastructure.database import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            async with db.begin():
                if apply:
                    model = await register_kling_4(db)
                    status = model.status
                    plan = await price_plan(db)
                else:
                    status = "draft"
                    plan = await price_plan(db)
        result = {
            "model": MODEL_SLUG,
            "applied": apply,
            "status": status,
            "retail_markup_percent": str((RETAIL_MULTIPLIER - 1) * 100),
            "prices": [payload.model_dump(mode="json") for payload in plan],
            "successful_smoke_claimed": False,
        }
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Register the draft model, reviewed capabilities and prices",
    )
    asyncio.run(run(apply=parser.parse_args().apply))
