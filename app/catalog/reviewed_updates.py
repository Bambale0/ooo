"""Apply a narrowly reviewed procurement change; never mutate retail or jobs."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.billing.fx import current_fx, snapshot
from app.catalog.models import PartnerPrice, PartnerPriceHistory


async def apply_procurement_update(db, *, model_id, expected_costs, new_costs, source, apply=False):
    if not source.strip() or not new_costs or set(expected_costs) != set(new_costs):
        raise ValueError("invalid_procurement_review")
    if any(not isinstance(cost, Decimal) or not cost.is_finite() or cost <= 0 for cost in new_costs.values()):
        raise ValueError("invalid_procurement_rate")
    rows = list(
        await db.scalars(
            select(PartnerPrice)
            .where(
                PartnerPrice.model_id == model_id,
                PartnerPrice.mode == "default",
                PartnerPrice.resolution.in_(new_costs),
            )
            .order_by(PartnerPrice.resolution)
            .with_for_update()
        )
    )
    if {row.resolution for row in rows} != set(new_costs):
        raise HTTPException(409, "procurement_variants_missing")
    for row in rows:
        if row.billing_unit != "second" or row.provider_cost_usdt not in {
            expected_costs[row.resolution],
            new_costs[row.resolution],
        }:
            raise HTTPException(409, "procurement_review_conflict")
    fx = await current_fx(db)
    changed = [row for row in rows if row.provider_cost_usdt != new_costs[row.resolution]]
    if apply:
        for row in changed:
            db.add(
                PartnerPriceHistory(
                    model_id=model_id,
                    mode=row.mode,
                    resolution=row.resolution,
                    old_price_rub=row.price_rub,
                    new_price_rub=row.price_rub,
                    old_provider_cost_usdt=row.provider_cost_usdt,
                    new_provider_cost_usdt=new_costs[row.resolution],
                    billing_unit=row.billing_unit,
                    rub_per_usdt_snapshot=fx["rate"],
                    fx_snapshot={**snapshot(fx), "procurement_source": source},
                )
            )
            row.provider_cost_usdt = new_costs[row.resolution]
        await db.flush()
    return {
        "updated": [row.resolution for row in changed] if apply else [],
        "pending": [row.resolution for row in changed] if not apply else [],
        "retail_prices_changed": False,
        "economics": [
            {
                "resolution": row.resolution,
                "price_rub_per_second": str(row.price_rub),
                "cost_usdt_per_second": str(new_costs[row.resolution]),
                "margin_pct": str(
                    (row.price_rub / fx["rate"] - new_costs[row.resolution]) / (row.price_rub / fx["rate"]) * 100
                )
                if row.price_rub
                else None,
            }
            for row in rows
        ],
        "fx": snapshot(fx),
    }
