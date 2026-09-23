"""Import reviewed procurement contracts without inventing retail prices."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory
from app.contracts.registry import CATALOG, MODELS, OBSERVATIONS
from app.infrastructure.config import get_settings
from app.providers.http_client import get_provider_http_client
from app.providers.models import ProviderModelCapability


async def check_catalog_drift():
    response = await get_provider_http_client("argolink").get("/api/catalog/v1/models?page_size=100")
    response.raise_for_status()
    import json

    data = json.loads(response.text, parse_float=Decimal)
    live = {m["id"]: m for m in data["items"]}
    changed = []
    for slug, saved in MODELS.items():
        if slug not in live:
            continue
        current = live[slug]
        effective = {k: v for k, v in current["pricing"]["effective"].items() if v is not None}
        if (
            saved["category"] != current["category"]
            or saved["endpoint"] != current["endpoint"]
            or saved["procurement"] != effective
        ):
            changed.append(slug)
    return {
        "revision": data["revision"],
        "reviewed_revision": CATALOG["revision"],
        "added": sorted(live.keys() - MODELS.keys()),
        "removed": sorted(MODELS.keys() - live.keys()),
        "changed": sorted(changed),
    }


def variants(entry):
    price = entry["procurement"]
    if entry["category"] == "chat":
        for mode, source in [
            ("input_tokens", "input_per_million"),
            ("output_tokens", "output_per_million"),
            ("cached_input_tokens", "cached_input_per_million"),
            ("cache_write_tokens", "cache_write_per_million"),
        ]:
            # A cache miss uses input pricing; cache-write pricing must be explicit
            # before enabling a route that supports paid cache creation.
            value = price.get(source, price.get("input_per_million"))
            yield mode, "default", "million_tokens", Decimal(str(value))
    else:
        for tier in price["tiers"]:
            yield (
                "default",
                tier["label"],
                "second" if entry["category"] == "video" else "generation",
                Decimal(str(tier["price"])),
            )


async def import_reviewed_catalog(db):
    drift = await check_catalog_drift()
    if drift["added"] or drift["removed"] or drift["changed"]:
        raise HTTPException(409, {"code": "catalog_review_required", **drift})
    created, updated_costs = [], []
    for slug, entry in MODELS.items():
        model = (await db.execute(select(Model).where(Model.slug == slug).with_for_update())).scalar_one_or_none()
        if model is None:
            model = Model(
                slug=slug,
                name=slug,
                modality={"chat": "llm", "image": "image", "video": "video"}[entry["category"]],
                status="draft",
                has_provider_integration=True,
                has_public_docs=True,
                has_successful_smoke=False,
            )
            db.add(model)
            await db.flush()
            created.append(slug)
        for mode, resolution, unit, cost in variants(entry):
            price = (
                await db.execute(
                    select(PartnerPrice)
                    .where(
                        PartnerPrice.model_id == model.id,
                        PartnerPrice.mode == mode,
                        PartnerPrice.resolution == resolution,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                slug not in OBSERVATIONS["manual_procurement_review"]
                and price is not None
                and (price.provider_cost_usdt != cost or price.billing_unit != unit)
            ):
                db.add(
                    PartnerPriceHistory(
                        model_id=model.id,
                        mode=mode,
                        resolution=resolution,
                        old_price_rub=price.price_rub,
                        new_price_rub=price.price_rub,
                        old_provider_cost_usdt=price.provider_cost_usdt,
                        new_provider_cost_usdt=cost,
                        billing_unit=unit,
                        rub_per_usdt_snapshot=get_settings().rub_per_usdt,
                    )
                )
                price.provider_cost_usdt, price.billing_unit = cost, unit
                updated_costs.append(f"{slug}:{mode}:{resolution}")
            capability = (
                await db.execute(
                    select(ProviderModelCapability).where(
                        ProviderModelCapability.provider == "argolink",
                        ProviderModelCapability.model_id == model.id,
                        ProviderModelCapability.mode == mode,
                        ProviderModelCapability.resolution == resolution,
                    )
                )
            ).scalar_one_or_none()
            if capability is None:
                db.add(
                    ProviderModelCapability(
                        provider="argolink", model_id=model.id, mode=mode, resolution=resolution, is_active=True
                    )
                )
    await db.flush()
    return {
        "created_models": created,
        "updated_procurement": updated_costs,
        "retail_prices_changed": False,
        "manual_procurement_review": OBSERVATIONS["manual_procurement_review"],
        "reviewed_revision": CATALOG["revision"],
    }
