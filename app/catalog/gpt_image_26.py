"""Scoped GPT Image 2.6 registration; run with --apply after reviewing the plan."""

import argparse
import asyncio
import json
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.billing.fx import current_fx
from app.catalog.models import Model, PartnerPrice
from app.catalog.router import upsert_price
from app.catalog.schemas import PartnerPriceUpsert
from app.contracts.registry import MODELS
from app.providers.models import ProviderModelCapability

MODEL_SLUGS = ("gpt-image-2.6-flare", "gpt-image-2.6-sunburst")
RETAIL_MULTIPLIER = Decimal("1.35")


async def price_plan(db):
    fx = (await current_fx(db))["rate"]
    return [
        PartnerPriceUpsert(
            model_slug=slug,
            resolution=tier["label"],
            provider_cost_usdt=Decimal(str(tier["price"])),
            price_rub=(Decimal(str(tier["price"])) * fx * RETAIL_MULTIPLIER).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
        )
        for slug in MODEL_SLUGS
        for tier in MODELS[slug]["procurement"]["tiers"]
    ]


async def register_gpt_image_26(db):
    """Use one caller-owned transaction; never enable a model or claim a smoke."""
    plan = await price_plan(db)
    for slug in MODEL_SLUGS:
        model = await db.scalar(select(Model).where(Model.slug == slug).with_for_update())
        if model is None:
            model = Model(
                slug=slug, name=f"GPT Image 2.6 {slug.rsplit('-', 1)[-1].title()}", modality="image",
                status="draft", has_provider_integration=True, has_public_docs=True, has_successful_smoke=False,
            )
            db.add(model)
            await db.flush()
        elif model.modality != "image":
            raise ValueError("model_modality_conflict")
        for payload in (p for p in plan if p.model_slug == slug):
            price = await db.scalar(
                select(PartnerPrice).where(
                    PartnerPrice.model_id == model.id,
                    PartnerPrice.mode == payload.mode,
                    PartnerPrice.resolution == payload.resolution,
                ).with_for_update()
            )
            # Avoid duplicate history on retry; writes reuse the normal snapshot
            # and negotiated-price protections instead of replacing rows.
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
                db.add(ProviderModelCapability(
                    provider="argolink", model_id=model.id, mode=payload.mode,
                    resolution=payload.resolution, is_active=True,
                ))
    await db.flush()
    return plan


async def run(*, apply=False):
    from app.infrastructure.database import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            async with db.begin():
                plan = await register_gpt_image_26(db) if apply else await price_plan(db)
        print(json.dumps({"applied": apply, "prices": [p.model_dump(mode="json") for p in plan]}, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit this model-only registration and price plan")
    asyncio.run(run(apply=parser.parse_args().apply))
