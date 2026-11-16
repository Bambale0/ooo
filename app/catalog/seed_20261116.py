"""Scoped draft registration for ArgoLink Seedance 2.5 Flash and Seedream 5.5.

Adds provider capabilities but deliberately does not invent retail prices,
claim a paid smoke, or publish models to partners. Run from the release image:
    python -m app.catalog.seed_20261116          # preview only
    python -m app.catalog.seed_20261116 --apply  # register drafts
"""

import argparse
import asyncio
import json

from sqlalchemy import select

from app.catalog.models import Model
from app.catalog.sync import variants
from app.contracts.registry import MODELS
from app.providers.models import ProviderModelCapability

SEED_MODELS = ("seedance-2.5-flash", "seedream-5.5")


async def register_seed_models(db) -> list[Model]:
    models: list[Model] = []
    for slug in SEED_MODELS:
        entry = MODELS[slug]
        modality = {"image": "image", "video": "video"}[entry["category"]]
        model = await db.scalar(select(Model).where(Model.slug == slug).with_for_update())
        if model is None:
            model = Model(
                slug=slug,
                name={"seedance-2.5-flash": "Seedance 2.5 Flash", "seedream-5.5": "Seedream 5.5"}[slug],
                modality=modality,
                status="draft",
                has_provider_integration=True,
                has_public_docs=True,
                has_successful_smoke=False,
            )
            db.add(model)
            await db.flush()
        elif model.modality != modality:
            raise ValueError("model_modality_conflict")
        else:
            model.has_provider_integration = True
            model.has_public_docs = True

        for mode, resolution, _unit, _cost in variants(entry):
            capability = await db.scalar(
                select(ProviderModelCapability).where(
                    ProviderModelCapability.provider == "argolink",
                    ProviderModelCapability.model_id == model.id,
                    ProviderModelCapability.mode == mode,
                    ProviderModelCapability.resolution == resolution,
                )
            )
            if capability is None:
                db.add(
                    ProviderModelCapability(
                        provider="argolink",
                        model_id=model.id,
                        mode=mode,
                        resolution=resolution,
                        is_active=True,
                    )
                )
        models.append(model)
    await db.flush()
    return models


async def run(*, apply: bool = False) -> None:
    if apply:
        from app.infrastructure.database import SessionLocal, engine

        try:
            async with SessionLocal() as db:
                async with db.begin():
                    models = await register_seed_models(db)
                    states = {model.slug: model.status for model in models}
        finally:
            await engine.dispose()
    else:
        states = {slug: "draft (preview only)" for slug in SEED_MODELS}
    print(json.dumps({
        "applied": apply,
        "models": states,
        "procurement": {
            slug: [
                {"mode": mode, "resolution": resolution, "unit": unit, "usd": str(cost)}
                for mode, resolution, unit, cost in variants(MODELS[slug])
            ]
            for slug in SEED_MODELS
        },
        "retail_prices_changed": False,
        "successful_smoke_claimed": False,
        "production_enabled": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create drafts and capabilities")
    asyncio.run(run(apply=parser.parse_args().apply))
