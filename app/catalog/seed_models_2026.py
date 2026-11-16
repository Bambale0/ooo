"""Scoped ArgoLink Seedance 2.5 Flash / Seedream 5.5 registration.

Registers reviewed contracts as draft-only. Retail prices, paid smoke evidence and
public enablement require a separate authorized release decision.
"""

import argparse
import asyncio
import json

from sqlalchemy import select

from app.catalog.models import Model
from app.catalog.sync import variants
from app.contracts.registry import MODELS
from app.providers.models import ProviderModelCapability

MODEL_SLUGS = ("seedance-2.5-flash", "seedream-5.5")
NAMES = {
    "seedance-2.5-flash": "Seedance 2.5 Flash",
    "seedream-5.5": "Seedream 5.5",
}


def registration_plan():
    return {
        slug: {
            "name": NAMES[slug],
            "category": MODELS[slug]["category"],
            "required_prices": [
                {"mode": mode, "resolution": resolution, "billing_unit": unit, "provider_cost_usdt": str(cost)}
                for mode, resolution, unit, cost in variants(MODELS[slug])
            ],
            "release_status": "draft",
        }
        for slug in MODEL_SLUGS
    }


async def register_seed_models(db):
    """Caller owns the transaction; repeat invocations add no duplicate records."""
    plan = registration_plan()
    for slug, spec in plan.items():
        model = await db.scalar(select(Model).where(Model.slug == slug).with_for_update())
        if model is None:
            model = Model(
                slug=slug, name=spec["name"], modality=spec["category"],
                status="draft", has_provider_integration=True, has_public_docs=True,
                has_successful_smoke=False,
            )
            db.add(model)
            await db.flush()
        elif model.modality != spec["category"]:
            raise ValueError("model_modality_conflict")
        for variant in spec["required_prices"]:
            existing = await db.scalar(
                select(ProviderModelCapability).where(
                    ProviderModelCapability.provider == "argolink",
                    ProviderModelCapability.model_id == model.id,
                    ProviderModelCapability.mode == variant["mode"],
                    ProviderModelCapability.resolution == variant["resolution"],
                )
            )
            if existing is None:
                db.add(ProviderModelCapability(
                    provider="argolink", model_id=model.id,
                    mode=variant["mode"], resolution=variant["resolution"],
                    is_active=True, billing_unit=variant["billing_unit"],
                ))
    await db.flush()
    return plan


async def run(*, apply=False):
    from app.infrastructure.database import SessionLocal, engine

    try:
        if apply:
            async with SessionLocal() as db:
                async with db.begin():
                    plan = await register_seed_models(db)
        else:
            plan = registration_plan()
        print(json.dumps({"applied": apply, "models": plan}, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Register draft models and provider capabilities only")
    asyncio.run(run(apply=parser.parse_args().apply))
