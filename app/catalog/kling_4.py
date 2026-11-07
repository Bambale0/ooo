"""Scoped Kling 4.0 registration without prices, smoke claims, or enablement."""

import argparse
import asyncio
import json

from sqlalchemy import select

from app.catalog.models import Model
from app.catalog.sync import variants
from app.contracts.registry import MODELS
from app.providers.models import ProviderModelCapability

MODEL_SLUG = "kling-4.0"


async def register_kling_4(db) -> Model:
    """Register the reviewed contract and capabilities in one caller-owned transaction."""
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

    for mode, resolution, _unit, _cost in variants(MODELS[MODEL_SLUG]):
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
    await db.flush()
    return model


async def run(*, apply: bool = False) -> None:
    from app.infrastructure.database import SessionLocal, engine

    try:
        result = {
            "model": MODEL_SLUG,
            "applied": apply,
            "status": "draft",
            "partner_prices_created": False,
            "successful_smoke_claimed": False,
        }
        if apply:
            async with SessionLocal() as db:
                async with db.begin():
                    model = await register_kling_4(db)
                    result["status"] = model.status
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Register the draft model and reviewed capabilities")
    asyncio.run(run(apply=parser.parse_args().apply))
