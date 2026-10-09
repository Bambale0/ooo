"""Dry-run by default. Apply only the reviewed Seedance 2.5 change and statement."""

import argparse
import asyncio
import json
from decimal import Decimal

from sqlalchemy import select

from app.billing.provider_credits import backfill_primary_cost_estimates, record_supplier_credit
from app.catalog.models import Model
from app.catalog.reviewed_updates import apply_procurement_update
from app.contracts.registry import CATALOG, MODELS
from app.infrastructure.database import SessionLocal
from app.providers.argolink import ArgoLinkAdapter


async def run(*, apply=False):
    slug = "seedance-2.5"
    review = CATALOG["procurement_reviews"][slug]
    costs = {tier["label"]: Decimal(str(tier["price"])) for tier in MODELS[slug]["procurement"]["tiers"]}
    if apply:
        # Do not install a reviewed price that the live supplier has changed again.
        adapter = ArgoLinkAdapter()
        response = await adapter._client.get("/api/catalog/v1/models?page_size=100", headers=adapter._auth_headers())
        response.raise_for_status()
        live = json.loads(response.text, parse_float=Decimal)
        entry = next(item for item in live["items"] if item["id"] == slug)
        price = entry["pricing"]["effective"]
        if (price["currency"], price["unit"]) != ("USD", "second") or {
            tier["label"]: Decimal(str(tier["price"])) for tier in price["tiers"]
        } != costs:
            raise ValueError("supplier_procurement_changed_again")
    async with SessionLocal() as db:
        model = await db.scalar(select(Model).where(Model.slug == slug))
        if model is None:
            raise ValueError("seedance25_model_missing")
        result = await apply_procurement_update(
            db,
            model_id=model.id,
            expected_costs={key: Decimal(value) for key, value in review["expected_previous"].items()},
            new_costs=costs,
            source=review["source"],
            apply=apply,
        )
        result["supplier_credit"] = review["compensation"]
        if apply:
            credit = review["compensation"]
            row = await record_supplier_credit(
                db,
                provider="argolink",
                reference=credit["reference"],
                amount_usdt=Decimal(credit["amount_usdt"]),
                evidence=review["source"],
                reason=credit["reason"],
            )
            result["supplier_credit_record_id"] = row.id
            result["backfilled_primary_estimates"] = await backfill_primary_cost_estimates(db)
            await db.commit()
        else:
            await db.rollback()
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(apply=parser.parse_args().apply))
